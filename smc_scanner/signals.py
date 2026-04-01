# signals.py — Full signal generation: combines context + liquidity + structure

from __future__ import annotations

import pandas as pd

import config
from liquidity import (LiquidityZone, SweepEvent,
                       build_liquidity_zones, detect_sweeps)
from structure import (BOSEvent, find_swings,
                       get_last_bos, get_market_context)
from utils import pct_diff


# ── Signal dict keys ───────────────────────────────────────────────────────────
# symbol, timeframe, context, sweep_desc, bos_desc,
# direction, entry_low, entry_high, stop, tp, reason


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
        return None   # BOS must come after the sweep

    swept_low = sweep.zone.level

    # Entry zone: current candle's close ±0.1 % or between sweep close and BOS level
    last_close = df["close"].iloc[-1]
    entry_low  = min(sweep.sweep_close, bos.broken_level)
    entry_high = max(sweep.sweep_close, bos.broken_level)

    # Make sure entry zone is sane
    if entry_high <= entry_low:
        entry_high = entry_low * 1.001

    # Stop: below the swept wick low + buffer
    stop = sweep.sweep_low * (1 - config.DEFAULT_SL_BUFFER)

    # TP: risk × RR_ratio projected up from mid-entry
    mid_entry  = (entry_low + entry_high) / 2
    risk       = mid_entry - stop
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

    swept_high = sweep.zone.level

    entry_high = max(sweep.sweep_close, bos.broken_level)
    entry_low  = min(sweep.sweep_close, bos.broken_level)

    if entry_high <= entry_low:
        entry_low = entry_high * 0.999

    # Stop: above swept wick high + buffer
    stop = sweep.sweep_high * (1 + config.DEFAULT_SL_BUFFER)

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


# ── Main scanner entry point ───────────────────────────────────────────────────

def scan_for_signals(symbol: str, timeframe: str, df: pd.DataFrame) -> list[dict]:
    """
    Run the full SMC analysis pipeline on a single (symbol, timeframe) pair.

    Returns a list of signal dicts (usually 0 or 1 per call).
    """
    if len(df) < 60:
        return []   # not enough data

    # Step 1 — Market context
    df_s = find_swings(df)
    context = get_market_context(df_s)

    # Step 2 — Liquidity zones
    zones = build_liquidity_zones(df_s)

    # Step 3 — Sweep events
    sweeps = detect_sweeps(df_s, zones)
    if not sweeps:
        return []

    # Step 4 — BOS / MBOS events
    bos_events = _get_bos_events_after(df_s, sweeps[-1].candle_idx)
    if not bos_events:
        return []

    # Step 5 — Signal generation
    signals: list[dict] = []
    last_sweep = sweeps[-1]

    for bos in bos_events:
        sig = None
        if last_sweep.direction == "down":
            # Swept lows → look for long
            sig = _build_long_signal(symbol, timeframe, context,
                                     last_sweep, bos, df_s)
        elif last_sweep.direction == "up":
            # Swept highs → look for short
            sig = _build_short_signal(symbol, timeframe, context,
                                      last_sweep, bos, df_s)

        if sig:
            signals.append(sig)
            break   # one signal per sweep event is enough

    return signals


# ── Helper ─────────────────────────────────────────────────────────────────────

def _get_bos_events_after(df: pd.DataFrame, after_idx: int) -> list[BOSEvent]:
    """Return BOS events that occurred after `after_idx`."""
    from structure import detect_bos
    all_events = detect_bos(df)
    return [e for e in all_events if e.candle_idx > after_idx]