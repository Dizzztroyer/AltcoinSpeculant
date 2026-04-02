# scoring.py — Signal quality scoring (0-100)
#
# Scoring table:
#   +40  valid setup baseline (sweep + BOS confirmed)
#   +15  RR >= MIN_RR_FOR_BONUS
#   + 5  RR >= STRONG_RR_BONUS (additional)
#   +15  HTF bias aligned with signal direction
#   +10  volume spike on sweep or BOS candle
#   +10  clear sweep (wick covers >= 70 % of candle range)
#   -10  flat / dead market (very low ATR)
#   -10  target too close to entry (< 1 % distance)
#   -20  duplicate signal in DEDUP_LOOKBACK_HOURS window
#
# Final score is clamped to [0, 100].

import numpy as np
import pandas as pd

import config
import journal
from datafeed import fetch_ohlcv
from structure import get_market_context, find_swings
from utils import atr, log_info


# ── Public entry point ────────────────────────────────────────────────────────

def score_signal(sig: dict, df: pd.DataFrame) -> tuple[int, str]:
    """
    Score a freshly generated signal dict.

    Parameters
    ----------
    sig : dict   — signal from signals.scan_for_signals()
    df  : DataFrame — the OHLCV data the signal was generated from

    Returns
    -------
    (score: int, higher_tf_bias: str)
    """
    points  = 0
    reasons = []

    # ── Baseline: setup is structurally valid ─────────────────────────────────
    points += 40
    reasons.append("+40 valid setup")

    # ── RR quality ────────────────────────────────────────────────────────────
    mid_entry = (sig["entry_low"] + sig["entry_high"]) / 2
    risk      = abs(mid_entry - sig["stop"])
    if risk > 0:
        rr = abs(sig["tp"] - mid_entry) / risk
        if rr >= config.STRONG_RR_BONUS:
            points += 20
            reasons.append(f"+20 RR {rr:.2f} (strong)")
        elif rr >= config.MIN_RR_FOR_BONUS:
            points += 15
            reasons.append(f"+15 RR {rr:.2f}")
        else:
            reasons.append(f" 0  RR {rr:.2f} (below threshold)")
    else:
        reasons.append(" 0  invalid risk (zero)")

    # ── Higher timeframe alignment ────────────────────────────────────────────
    htf_bias = _get_htf_bias(sig["symbol"], sig["timeframe"])
    if htf_bias:
        if _bias_aligns(sig["direction"], htf_bias):
            points += 15
            reasons.append(f"+15 HTF ({htf_bias}) aligned")
        else:
            points -= 10
            reasons.append(f"-10 HTF ({htf_bias}) opposing")
    else:
        reasons.append(" 0  HTF bias unavailable")

    # ── Volume confirmation ────────────────────────────────────────────────────
    if config.ENABLE_VOLUME_CONFIRMATION:
        vol_ok = _volume_confirmed(df)
        if vol_ok:
            points += 10
            reasons.append("+10 volume spike confirmed")
        else:
            reasons.append(" 0  no volume spike")

    # ── Sweep clarity ─────────────────────────────────────────────────────────
    sweep_clear = _sweep_is_clear(df)
    if sweep_clear:
        points += 10
        reasons.append("+10 clear sweep rejection")
    else:
        reasons.append(" 0  sweep not crisp")

    # ── Volatility sanity ─────────────────────────────────────────────────────
    atr_val = _get_atr(df)
    if atr_val is not None and atr_val < mid_entry * 0.001:
        points -= 10
        reasons.append("-10 dead market (ATR very low)")

    # ── Target distance penalty ───────────────────────────────────────────────
    tp_dist = abs(sig["tp"] - mid_entry) / mid_entry if mid_entry else 0
    if tp_dist < 0.01:
        points -= 10
        reasons.append("-10 TP too close to entry (< 1 %)")

    # ── Deduplication penalty ─────────────────────────────────────────────────
    recent = journal.get_recent_signals(
        sig["symbol"], sig["timeframe"], sig["direction"],
        config.DEDUP_LOOKBACK_HOURS
    )
    if recent:
        points -= 20
        reasons.append(f"-20 duplicate (seen {len(recent)}x in last {config.DEDUP_LOOKBACK_HOURS}h)")

    # ── Clamp ─────────────────────────────────────────────────────────────────
    score = max(0, min(100, points))
    log_info(f"[SCORE] {sig['symbol']} {sig['timeframe']} {sig['direction'].upper()} "
             f"→ {score}/100  |  " + "  ".join(reasons))

    return score, htf_bias or ""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_htf_bias(symbol: str, timeframe: str) -> str:
    """Fetch the higher timeframe and return 'bullish', 'bearish', or 'range'."""
    htf = config.HTF_MAP.get(timeframe)
    if not htf:
        return ""
    try:
        df_htf = fetch_ohlcv(symbol, htf, limit=100)
        if df_htf.empty:
            return ""
        df_htf = find_swings(df_htf)
        return get_market_context(df_htf)
    except Exception:
        return ""


def _bias_aligns(direction: str, bias: str) -> bool:
    """True if the signal direction matches the HTF bias."""
    if direction == "long"  and bias == "bullish":
        return True
    if direction == "short" and bias == "bearish":
        return True
    # Range context is considered neutral — slight positive for the signal direction
    if bias == "range":
        return True   # neutral → no penalty, no bonus → handled by returning True
    return False


def _volume_confirmed(df: pd.DataFrame) -> bool:
    """
    Check whether the last few candles show a volume spike vs rolling average.
    Uses the most recent config.VOLUME_LOOKBACK bars.
    """
    if "volume" not in df.columns or len(df) < config.VOLUME_LOOKBACK + 2:
        return False
    avg_vol   = df["volume"].iloc[-(config.VOLUME_LOOKBACK + 1):-1].mean()
    last_vol  = df["volume"].iloc[-1]
    prev_vol  = df["volume"].iloc[-2]
    threshold = avg_vol * config.VOLUME_SPIKE_MULTIPLIER
    return last_vol >= threshold or prev_vol >= threshold


def _sweep_is_clear(df: pd.DataFrame) -> bool:
    """
    A sweep candle is 'clear' when its wick covers >= 70 % of the full range.
    We look at the last 5 candles for such a candle.
    """
    recent = df.tail(5)
    for _, row in recent.iterrows():
        c_range = row["high"] - row["low"]
        if c_range == 0:
            continue
        body    = abs(row["close"] - row["open"])
        wick    = c_range - body
        if wick / c_range >= 0.70:
            return True
    return False


def _get_atr(df: pd.DataFrame) -> float | None:
    """Return the last ATR(14) value."""
    try:
        from utils import atr as compute_atr
        a = compute_atr(df, 14)
        return a.iloc[-1] if not a.empty else None
    except Exception:
        return None