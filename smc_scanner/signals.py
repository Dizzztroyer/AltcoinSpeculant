# signals.py — Signal generation with Order Block + FVG entry zones
#
# Pipeline:
#   1. Market context
#   2. Liquidity zones
#   3. Sweep detection
#   4. BOS / MBOS confirmation
#   5. Order Block detection
#   6. OB-refined entry zone  ← replaces the old rough entry
#   7. HTF confluence filter
#   8. Attach all metadata

from __future__ import annotations

import pandas as pd

import config
from liquidity import build_liquidity_zones, detect_sweeps, SweepEvent
from orderblocks import (OrderBlock, find_fvgs, find_order_blocks,
                         get_nearest_ob)
from structure import (BOSEvent, HTFConfluence, find_swings,
                       get_htf_confluence, get_market_context)
from utils import log_info, pct_diff


# ── Long signal builder ────────────────────────────────────────────────────────

def _build_long_signal(symbol: str, timeframe: str,
                       context: str,
                       sweep: SweepEvent,
                       bos: BOSEvent,
                       ob: OrderBlock | None,
                       df: pd.DataFrame) -> dict | None:
    if sweep.direction != "down":
        return None
    if bos.direction != "bullish":
        return None
    if bos.candle_idx <= sweep.candle_idx:
        return None

    current_price = df["close"].iloc[-1]

    # ── Entry zone ────────────────────────────────────────────────────────────
    if ob is not None and not ob.mitigated:
        # Precise OB-based entry
        entry_low  = ob.entry_low
        entry_high = ob.entry_high
        stop       = ob.low * (1 - config.DEFAULT_SL_BUFFER)
        ob_label   = ob.label()
    else:
        # Fallback: old sweep-to-BOS range
        entry_low  = min(sweep.sweep_close, bos.broken_level)
        entry_high = max(sweep.sweep_close, bos.broken_level)
        stop       = sweep.sweep_low * (1 - config.DEFAULT_SL_BUFFER)
        ob_label   = None

    if entry_high <= entry_low:
        entry_high = entry_low * 1.001

    mid_entry = (entry_low + entry_high) / 2
    risk      = mid_entry - stop
    if risk <= 0:
        return None

    tp = mid_entry + risk * config.DEFAULT_RR_RATIO

    # Build reason string
    if ob_label:
        fvg_note = " (FVG overlap)" if ob is not None and ob.has_fvg else ""
        reason = (
            f"Swept {sweep.zone.label()}, bullish {bos.label()} confirmed. "
            f"Entry refined to {ob_label}{fvg_note}"
        )
    else:
        reason = (
            f"Swept {sweep.zone.label()}, closed back above, "
            f"{bos.label()} confirmed → bullish continuation. "
            f"No OB found — using sweep/BOS range."
        )

    return dict(
        symbol=symbol, timeframe=timeframe,
        context=context.title(),
        sweep_desc=sweep.label(),
        bos_desc=bos.label(),
        direction="long",
        entry_low=entry_low, entry_high=entry_high,
        stop=stop, tp=tp, reason=reason,
        ob_label=ob_label or "",
        ob_has_fvg=ob.has_fvg if ob else False,
    )


# ── Short signal builder ───────────────────────────────────────────────────────

def _build_short_signal(symbol: str, timeframe: str,
                        context: str,
                        sweep: SweepEvent,
                        bos: BOSEvent,
                        ob: OrderBlock | None,
                        df: pd.DataFrame) -> dict | None:
    if sweep.direction != "up":
        return None
    if bos.direction != "bearish":
        return None
    if bos.candle_idx <= sweep.candle_idx:
        return None

    current_price = df["close"].iloc[-1]

    if ob is not None and not ob.mitigated:
        entry_low  = ob.entry_low
        entry_high = ob.entry_high
        stop       = ob.high * (1 + config.DEFAULT_SL_BUFFER)
        ob_label   = ob.label()
    else:
        entry_high = max(sweep.sweep_close, bos.broken_level)
        entry_low  = min(sweep.sweep_close, bos.broken_level)
        stop       = sweep.sweep_high * (1 + config.DEFAULT_SL_BUFFER)
        ob_label   = None

    if entry_high <= entry_low:
        entry_low = entry_high * 0.999

    mid_entry = (entry_low + entry_high) / 2
    risk      = stop - mid_entry
    if risk <= 0:
        return None

    tp = mid_entry - risk * config.DEFAULT_RR_RATIO

    if ob_label:
        fvg_note = " (FVG overlap)" if ob is not None and ob.has_fvg else ""
        reason = (
            f"Swept {sweep.zone.label()}, bearish {bos.label()} confirmed. "
            f"Entry refined to {ob_label}{fvg_note}"
        )
    else:
        reason = (
            f"Swept {sweep.zone.label()}, closed back below, "
            f"{bos.label()} confirmed → bearish continuation. "
            f"No OB found — using sweep/BOS range."
        )

    return dict(
        symbol=symbol, timeframe=timeframe,
        context=context.title(),
        sweep_desc=sweep.label(),
        bos_desc=bos.label(),
        direction="short",
        entry_low=entry_low, entry_high=entry_high,
        stop=stop, tp=tp, reason=reason,
        ob_label=ob_label or "",
        ob_has_fvg=ob.has_fvg if ob else False,
    )


# ── HTF filter ─────────────────────────────────────────────────────────────────

def _apply_htf_filter(sig: dict) -> tuple[bool, HTFConfluence | None]:
    if not config.HTF_FILTER_ENABLED:
        return True, None

    confluence = get_htf_confluence(
        symbol=sig["symbol"],
        signal_direction=sig["direction"],
        signal_timeframe=sig["timeframe"],
    )

    if confluence is None:
        log_info(f"[HTF] data unavailable for {sig['symbol']} — filter skipped")
        return True, None

    log_info(f"[HTF] {sig['symbol']} {sig['timeframe']} "
             f"{sig['direction'].upper()} — {confluence.reason}")

    if config.HTF_FILTER_STRICT and confluence.opposing:
        log_info(f"[HTF] BLOCKED — opposes {confluence.htf_tf} ({confluence.bias})")
        return False, confluence

    return True, confluence


# ── Main scanner ───────────────────────────────────────────────────────────────

def scan_for_signals(symbol: str, timeframe: str, df: pd.DataFrame) -> list[dict]:
    """
    Full SMC pipeline with OB-refined entries.

    Returns list of signal dicts. Each dict contains:
      Standard:  symbol, timeframe, context, direction,
                 entry_low, entry_high, stop, tp, reason,
                 sweep_desc, bos_desc
      OB fields: ob_label, ob_has_fvg
      HTF fields: htf_bias, htf_aligned, htf_opposing, htf_tf, htf_reason
    """
    if len(df) < 60:
        return []

    df_s    = find_swings(df)
    context = get_market_context(df_s)

    zones  = build_liquidity_zones(df_s)
    sweeps = detect_sweeps(df_s, zones)
    if not sweeps:
        return []

    bos_events = _get_bos_events_after(df_s, sweeps[-1].candle_idx)
    if not bos_events:
        return []

    # Pre-compute OBs and FVGs once for the whole df
    fvgs = find_fvgs(df_s)
    obs  = find_order_blocks(df_s, fvgs=fvgs)

    if obs:
        unmitigated = [o for o in obs if not o.mitigated]
        log_info(f"[OB] {symbol} {timeframe} — "
                 f"{len(obs)} OBs found, {len(unmitigated)} unmitigated, "
                 f"{len(fvgs)} FVGs")

    signals: list[dict] = []
    last_sweep = sweeps[-1]
    current_price = df_s["close"].iloc[-1]

    for bos in bos_events:
        # Find the most relevant OB for this signal direction
        direction = ("long"  if last_sweep.direction == "down" else "short")
        ob = get_nearest_ob(obs, direction, current_price)

        if ob:
            log_info(f"[OB] using {ob.label()} for {direction.upper()} entry")
        else:
            log_info(f"[OB] no suitable OB found — falling back to sweep/BOS zone")

        sig = None
        if direction == "long":
            sig = _build_long_signal(symbol, timeframe, context,
                                     last_sweep, bos, ob, df_s)
        else:
            sig = _build_short_signal(symbol, timeframe, context,
                                      last_sweep, bos, ob, df_s)

        if not sig:
            continue

        # HTF filter
        allowed, confluence = _apply_htf_filter(sig)
        if not allowed:
            continue

        # Attach HTF metadata
        if confluence:
            sig.update(dict(
                htf_bias=confluence.bias,
                htf_aligned=confluence.aligned,
                htf_opposing=confluence.opposing,
                htf_tf=confluence.htf_tf,
                htf_reason=confluence.reason,
            ))
        else:
            sig.update(dict(
                htf_bias="", htf_aligned=False,
                htf_opposing=False, htf_tf="",
                htf_reason="HTF filter disabled or data unavailable",
            ))

        signals.append(sig)
        break

    return signals


def _get_bos_events_after(df: pd.DataFrame, after_idx: int) -> list[BOSEvent]:
    from structure import detect_bos
    return [e for e in detect_bos(df) if e.candle_idx > after_idx]