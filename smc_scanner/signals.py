# signals.py — SMC signal generator with strict multi-layer confirmation
#
# Pipeline:
#   1.  Market context + swing detection
#   2.  Liquidity zones
#   3.  Sweep detection
#   4.  BOS detection
#   5.  OB / FVG computation
#   6.  Run ALL 6 confirmation layers via confirmation.py
#   7.  Build signal only if CheckResult.allowed == True
#   8.  Entry zone = OB / FVG (no fallback to rough zone)
#   9.  Attach full confirmation metadata to signal dict

from __future__ import annotations

import pandas as pd

import config
from confirmation import CheckResult, run_confirmations
from liquidity import LiquidityZone, SweepEvent, build_liquidity_zones, detect_sweeps
from orderblocks import FairValueGap, OrderBlock, find_fvgs, find_order_blocks, get_nearest_ob
from structure import BOSEvent, find_swings, get_market_context, detect_bos
from utils import log_info, log_warn


# ── Entry zone builder ────────────────────────────────────────────────────────

def _build_entry(direction: str,
                 sweep: SweepEvent,
                 bos: BOSEvent,
                 obs: list[OrderBlock],
                 fvgs: list[FairValueGap],
                 df: pd.DataFrame) -> dict | None:
    """
    Build the entry parameters ONLY from OB / FVG.
    No fallback to rough sweep/BOS range — if no OB/FVG, return None.
    (confirmation.py already ensures at least one exists before we reach here)
    """
    current_price = df["close"].iloc[-1]
    ob = get_nearest_ob(obs, direction, current_price)

    if ob and not ob.mitigated:
        entry_low  = ob.entry_low
        entry_high = ob.entry_high
        stop       = (ob.low  * (1 - config.DEFAULT_SL_BUFFER) if direction == "long"
                      else ob.high * (1 + config.DEFAULT_SL_BUFFER))
        ob_label   = ob.label()
        ob_has_fvg = ob.has_fvg
    else:
        # OB not usable — try standalone FVG
        fvg_type   = "bullish" if direction == "long" else "bearish"
        candidates = [f for f in fvgs if f.fvg_type == fvg_type and not f.filled]
        if not candidates:
            return None   # no entry zone at all
        fvg        = candidates[-1]
        entry_low  = fvg.gap_low
        entry_high = fvg.gap_high
        stop       = (sweep.sweep_low  * (1 - config.DEFAULT_SL_BUFFER) if direction == "long"
                      else sweep.sweep_high * (1 + config.DEFAULT_SL_BUFFER))
        ob_label   = f"FVG [{fvg.gap_low:.2f}–{fvg.gap_high:.2f}]"
        ob_has_fvg = True

    if entry_high <= entry_low:
        entry_high = entry_low * 1.001

    mid_entry = (entry_low + entry_high) / 2
    risk      = abs(mid_entry - stop)
    if risk <= 0:
        return None

    tp = (mid_entry + risk * config.DEFAULT_RR_RATIO if direction == "long"
          else mid_entry - risk * config.DEFAULT_RR_RATIO)

    return dict(
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        tp=tp,
        ob_label=ob_label,
        ob_has_fvg=ob_has_fvg,
    )


# ── Main scanner ───────────────────────────────────────────────────────────────

def scan_for_signals(symbol: str, timeframe: str,
                     df: pd.DataFrame) -> list[dict]:
    """
    Full A+ SMC pipeline.

    Returns list of signal dicts — normally 0 or 1.
    Each dict includes:
      Core:         symbol, timeframe, direction, context
                    entry_low, entry_high, stop, tp
                    sweep_desc, bos_desc, ob_label, ob_has_fvg, reason
      Confirmation: conf_score, conf_passed, conf_failed,
                    conf_rejected_by, htf_bias, htf_aligned,
                    htf_opposing, htf_tf, htf_reason,
                    pd_zone, liq_target
    """
    if len(df) < 60:
        return []

    df_s    = find_swings(df)
    context = get_market_context(df_s)

    zones  = build_liquidity_zones(df_s)
    sweeps = detect_sweeps(df_s, zones)
    if not sweeps:
        log_info(f"[SCAN] {symbol} {timeframe}: no sweeps")
        return []

    bos_all    = detect_bos(df_s)
    last_sweep = sweeps[-1]
    bos_after  = [e for e in bos_all if e.candle_idx > last_sweep.candle_idx]

    if not bos_after:
        log_info(f"[SCAN] {symbol} {timeframe}: no BOS after sweep")
        return []

    fvgs = find_fvgs(df_s)
    obs  = find_order_blocks(df_s, fvgs=fvgs)

    direction = "long" if last_sweep.direction == "down" else "short"
    bos       = bos_after[0]   # first BOS after sweep

    # ── Direction pre-check ───────────────────────────────────────────────────
    if direction == "long" and bos.direction != "bullish":
        log_info(f"[SCAN] {symbol} {timeframe}: BOS direction mismatch")
        return []
    if direction == "short" and bos.direction != "bearish":
        log_info(f"[SCAN] {symbol} {timeframe}: BOS direction mismatch")
        return []

    # ── Run ALL confirmation layers ───────────────────────────────────────────
    result: CheckResult = run_confirmations(
        symbol=symbol, timeframe=timeframe, direction=direction,
        df=df_s, sweep=last_sweep,
        bos_candle_idx=bos.candle_idx,
        bos_level=bos.broken_level,
        obs=obs, fvgs=fvgs, zones=zones,
    )

    log_info(f"[CONF] {symbol} {timeframe} {direction.upper()} — {result.summary()}")
    for line in result.passed:
        log_info(f"[CONF]   ✅ {line}")
    for line in result.failed:
        log_info(f"[CONF]   ❌ {line}")

    if not result.allowed:
        log_info(f"[CONF] REJECTED by: {result.rejected_by}")
        return []

    # ── Build entry zone ──────────────────────────────────────────────────────
    entry = _build_entry(direction, last_sweep, bos, obs, fvgs, df_s)
    if entry is None:
        log_warn(f"[SCAN] {symbol} {timeframe}: confirmed but no entry zone — skip")
        return []

    # ── Adjust TP to liquidity target if available ────────────────────────────
    if result.liq_target is not None:
        mid_entry = (entry["entry_low"] + entry["entry_high"]) / 2
        risk      = abs(mid_entry - entry["stop"])
        # Only use liq target if it gives RR >= minimum
        target_rr = abs(result.liq_target - mid_entry) / risk if risk > 0 else 0
        if target_rr >= config.MIN_RR_FOR_BONUS:
            entry["tp"] = result.liq_target
            log_info(f"[SCAN] TP set to liquidity target {result.liq_target:.2f} "
                     f"(RR {target_rr:.2f})")

    # ── Assemble signal dict ──────────────────────────────────────────────────
    fvg_note = " with FVG overlap" if entry["ob_has_fvg"] else ""
    reason   = (
        f"Swept {last_sweep.label()} | {bos.label()} | "
        f"Entry: {entry['ob_label']}{fvg_note} | "
        f"HTF {result.htf_tf} {result.htf_bias} | "
        f"Zone: {result.pd_zone}"
    )

    sig = dict(
        # Core
        symbol=symbol, timeframe=timeframe,
        context=context.title(),
        direction=direction,
        sweep_desc=last_sweep.label(),
        bos_desc=bos.label(),
        reason=reason,
        # Entry
        entry_low=entry["entry_low"],
        entry_high=entry["entry_high"],
        stop=entry["stop"],
        tp=entry["tp"],
        ob_label=entry["ob_label"],
        ob_has_fvg=entry["ob_has_fvg"],
        # Confirmation metadata
        conf_score=result.score,
        conf_passed=result.passed,
        conf_failed=result.failed,
        conf_rejected_by="",
        pd_zone=result.pd_zone,
        liq_target=result.liq_target,
        # HTF (for scoring.py compatibility)
        htf_bias=result.htf_bias,
        htf_aligned=(result.htf_bias in ("bullish", "bearish") and
                     result.htf_tf != ""),
        htf_opposing=False,
        htf_tf=result.htf_tf,
        htf_reason=f"HTF {result.htf_tf} {result.htf_bias}",
    )

    return [sig]