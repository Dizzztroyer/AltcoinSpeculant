# signals.py — Full signal generation: combines context + liquidity + structure
#
# v2 change: HTF confluence is evaluated here as a hard filter.
# If HTF_FILTER_ENABLED=True and HTF_FILTER_STRICT=True, signals that directly
# oppose the higher-timeframe bias are dropped before scoring/saving.

from __future__ import annotations

import pandas as pd

import config
from liquidity import (LiquidityZone, SweepEvent,
                       build_liquidity_zones, detect_sweeps)
from structure import (BOSEvent, HTFConfluence, find_swings,
                       get_htf_confluence, get_last_bos, get_market_context)
from utils import log_info, pct_diff


# ── Long signal builder ────────────────────────────────────────────────────────

def _build_long_signal(symbol: str, timeframe: str,
                       context: str,
                       sweep: SweepEvent,
                       bos: BOSEvent,
                       df: pd.DataFrame) -> dict | None:
    """
    Long setup:
    • price sweeps below a low → bearish liquidity taken
    • price returns above that low
    • bullish BOS confirmed AFTER the sweep candle
    """
    if sweep.direction != "down":
        return None
    if bos.direction != "bullish":
        return None
    if bos.candle_idx <= sweep.candle_idx:
        return None

    entry_low  = min(sweep.sweep_close, bos.broken_level)
    entry_high = max(sweep.sweep_close, bos.broken_level)
    if entry_high <= entry_low:
        entry_high = entry_low * 1.001

    stop      = sweep.sweep_low * (1 - config.DEFAULT_SL_BUFFER)
    mid_entry = (entry_low + entry_high) / 2
    risk      = mid_entry - stop
    if risk <= 0:
        return None
    tp = mid_entry + risk * config.DEFAULT_RR_RATIO

    reason = (
        f"Swept {sweep.zone.label()}, closed back above, "
        f"then {bos.label()} confirmed → bullish continuation expected"
    )

    return dict(
        symbol=symbol,
        timeframe=timeframe,
        context=context.title(),
        sweep_desc=sweep.label(),
        bos_desc=bos.label(),
        direction="long",
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        tp=tp,
        reason=reason,
    )


# ── Short signal builder ───────────────────────────────────────────────────────

def _build_short_signal(symbol: str, timeframe: str,
                        context: str,
                        sweep: SweepEvent,
                        bos: BOSEvent,
                        df: pd.DataFrame) -> dict | None:
    """
    Short setup:
    • price sweeps above a high → bullish liquidity taken
    • price returns below that high
    • bearish BOS confirmed AFTER the sweep candle
    """
    if sweep.direction != "up":
        return None
    if bos.direction != "bearish":
        return None
    if bos.candle_idx <= sweep.candle_idx:
        return None

    entry_high = max(sweep.sweep_close, bos.broken_level)
    entry_low  = min(sweep.sweep_close, bos.broken_level)
    if entry_high <= entry_low:
        entry_low = entry_high * 0.999

    stop      = sweep.sweep_high * (1 + config.DEFAULT_SL_BUFFER)
    mid_entry = (entry_low + entry_high) / 2
    risk      = stop - mid_entry
    if risk <= 0:
        return None
    tp = mid_entry - risk * config.DEFAULT_RR_RATIO

    reason = (
        f"Swept {sweep.zone.label()}, closed back below, "
        f"then {bos.label()} confirmed → bearish continuation expected"
    )

    return dict(
        symbol=symbol,
        timeframe=timeframe,
        context=context.title(),
        sweep_desc=sweep.label(),
        bos_desc=bos.label(),
        direction="short",
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        tp=tp,
        reason=reason,
    )


# ── HTF confluence filter ──────────────────────────────────────────────────────

def _apply_htf_filter(sig: dict) -> tuple[bool, HTFConfluence | None]:
    """
    Check HTF confluence for a candidate signal.

    Returns
    -------
    (allowed: bool, confluence: HTFConfluence | None)

    Logic
    -----
    • HTF_FILTER_ENABLED = False  → always allowed, confluence=None
    • HTF_FILTER_STRICT  = False  → always allowed, confluence returned for scoring
    • HTF_FILTER_STRICT  = True   → blocked if confluence.opposing is True
    • If HTF data unavailable     → allowed (fail-open), confluence=None
    """
    if not config.HTF_FILTER_ENABLED:
        return True, None

    confluence = get_htf_confluence(
        symbol=sig["symbol"],
        signal_direction=sig["direction"],
        signal_timeframe=sig["timeframe"],
    )

    if confluence is None:
        # Data unavailable — fail open so we never silently drop signals
        log_info(f"[HTF] data unavailable for {sig['symbol']} — filter skipped")
        return True, None

    log_info(f"[HTF] {sig['symbol']} {sig['timeframe']} "
             f"{sig['direction'].upper()} — {confluence.reason}")

    if config.HTF_FILTER_STRICT and confluence.opposing:
        log_info(f"[HTF] signal BLOCKED — opposes {confluence.htf_tf} bias "
                 f"({confluence.bias})")
        return False, confluence

    return True, confluence


# ── Main scanner entry point ───────────────────────────────────────────────────

def scan_for_signals(symbol: str, timeframe: str, df: pd.DataFrame) -> list[dict]:
    """
    Run the full SMC analysis pipeline on a single (symbol, timeframe) pair.

    Pipeline
    --------
    1. Market context
    2. Liquidity zones
    3. Sweep detection
    4. BOS / MBOS confirmation
    5. Signal construction
    6. HTF confluence filter  ← new in v2
    7. Attach confluence data to signal dict

    Returns a list of signal dicts (usually 0 or 1 per call).
    Each dict gains two extra keys:
        htf_bias      : str   — 'bullish' | 'bearish' | 'range' | ''
        htf_aligned   : bool
        htf_opposing  : bool
        htf_tf        : str   — which HTF was consulted
        htf_reason    : str
    """
    if len(df) < 60:
        return []

    # Step 1 — Market context
    df_s    = find_swings(df)
    context = get_market_context(df_s)

    # Step 2 — Liquidity zones
    zones = build_liquidity_zones(df_s)

    # Step 3 — Sweep events
    sweeps = detect_sweeps(df_s, zones)
    if not sweeps:
        return []

    # Step 4 — BOS / MBOS events after the last sweep
    bos_events = _get_bos_events_after(df_s, sweeps[-1].candle_idx)
    if not bos_events:
        return []

    # Step 5 — Build candidate signal
    signals: list[dict] = []
    last_sweep = sweeps[-1]

    for bos in bos_events:
        sig = None
        if last_sweep.direction == "down":
            sig = _build_long_signal(symbol, timeframe, context,
                                     last_sweep, bos, df_s)
        elif last_sweep.direction == "up":
            sig = _build_short_signal(symbol, timeframe, context,
                                      last_sweep, bos, df_s)
        if not sig:
            continue

        # Step 6 — HTF confluence filter
        allowed, confluence = _apply_htf_filter(sig)
        if not allowed:
            continue   # hard block — signal dropped entirely

        # Step 7 — Attach confluence metadata to the signal dict
        if confluence:
            sig["htf_bias"]     = confluence.bias
            sig["htf_aligned"]  = confluence.aligned
            sig["htf_opposing"] = confluence.opposing
            sig["htf_tf"]       = confluence.htf_tf
            sig["htf_reason"]   = confluence.reason
        else:
            sig["htf_bias"]     = ""
            sig["htf_aligned"]  = False
            sig["htf_opposing"] = False
            sig["htf_tf"]       = ""
            sig["htf_reason"]   = "HTF filter disabled or data unavailable"

        signals.append(sig)
        break   # one signal per sweep event

    return signals


# ── Helper ─────────────────────────────────────────────────────────────────────

def _get_bos_events_after(df: pd.DataFrame, after_idx: int) -> list[BOSEvent]:
    """Return BOS events that occurred after `after_idx`."""
    from structure import detect_bos
    all_events = detect_bos(df)
    return [e for e in all_events if e.candle_idx > after_idx]