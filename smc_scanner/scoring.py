# scoring.py — Signal quality scoring (0-100)
#
# v2 change: HTF confluence is now evaluated upstream in signals.py and
# passed in via the signal dict (sig["htf_bias"], sig["htf_aligned"], etc.).
# scoring.py reads those fields directly — no extra API call here.
#
# Scoring table:
#   +40  valid setup baseline
#   +15  RR >= MIN_RR_FOR_BONUS
#   +20  RR >= STRONG_RR_BONUS  (replaces +15)
#   +15  HTF aligned with signal direction
#   - 0  HTF neutral (range) — no bonus, no penalty
#   -10  HTF opposing  (only reached when HTF_FILTER_STRICT=False)
#   +10  volume spike on recent candles
#   +10  clear sweep candle (wick >= 70 % of range)
#   -10  dead market (ATR very low relative to price)
#   -10  TP too close to entry (< 1 %)
#   -20  duplicate signal in DEDUP_LOOKBACK_HOURS window
#
# Score clamped to [0, 100].

import pandas as pd

import config
import journal
from utils import atr, log_info


# ── Public entry point ────────────────────────────────────────────────────────

def score_signal(sig: dict, df: pd.DataFrame) -> tuple[int, str]:
    """
    Score a freshly generated signal dict.

    Parameters
    ----------
    sig : dict        — from signals.scan_for_signals(); must contain htf_* keys
    df  : DataFrame   — OHLCV data the signal was generated from

    Returns
    -------
    (score: int, htf_bias: str)
    """
    points  = 0
    reasons = []

    # ── Baseline ──────────────────────────────────────────────────────────────
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

    # ── HTF confluence (pre-computed in signals.py — no extra API call) ───────
    htf_bias     = sig.get("htf_bias", "")
    htf_aligned  = sig.get("htf_aligned", False)
    htf_opposing = sig.get("htf_opposing", False)

    if htf_bias == "":
        reasons.append(" 0  HTF data unavailable")
    elif htf_aligned:
        points += 15
        reasons.append(f"+15 HTF {sig.get('htf_tf','')} aligned ({htf_bias})")
    elif htf_opposing:
        # Only reachable when HTF_FILTER_STRICT=False (strict mode blocks the signal)
        points -= 10
        reasons.append(f"-10 HTF {sig.get('htf_tf','')} opposing ({htf_bias})")
    else:
        # neutral / range
        reasons.append(f" 0  HTF {sig.get('htf_tf','')} neutral ({htf_bias})")

    # ── Volume confirmation ────────────────────────────────────────────────────
    if config.ENABLE_VOLUME_CONFIRMATION:
        if _volume_confirmed(df):
            points += 10
            reasons.append("+10 volume spike confirmed")
        else:
            reasons.append(" 0  no volume spike")

    # ── Sweep clarity ─────────────────────────────────────────────────────────
    if _sweep_is_clear(df):
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
        reasons.append("-10 TP too close (< 1 %)")

    # ── Deduplication penalty ─────────────────────────────────────────────────
    recent = journal.get_recent_signals(
        sig["symbol"], sig["timeframe"], sig["direction"],
        config.DEDUP_LOOKBACK_HOURS,
    )
    if recent:
        points -= 20
        reasons.append(f"-20 duplicate ({len(recent)}x in last {config.DEDUP_LOOKBACK_HOURS}h)")

    # ── Clamp & log ───────────────────────────────────────────────────────────
    score = max(0, min(100, points))
    log_info(
        f"[SCORE] {sig['symbol']} {sig['timeframe']} {sig['direction'].upper()} "
        f"→ {score}/100  |  " + "  ".join(reasons)
    )

    return score, htf_bias


# ── Helpers ───────────────────────────────────────────────────────────────────

def _volume_confirmed(df: pd.DataFrame) -> bool:
    """Volume spike on either of the last 2 candles vs rolling average."""
    if "volume" not in df.columns or len(df) < config.VOLUME_LOOKBACK + 2:
        return False
    avg_vol   = df["volume"].iloc[-(config.VOLUME_LOOKBACK + 1):-1].mean()
    threshold = avg_vol * config.VOLUME_SPIKE_MULTIPLIER
    return (df["volume"].iloc[-1] >= threshold or
            df["volume"].iloc[-2] >= threshold)


def _sweep_is_clear(df: pd.DataFrame) -> bool:
    """Wick-dominant candle (wick >= 70 % of range) in last 5 bars."""
    for _, row in df.tail(5).iterrows():
        c_range = row["high"] - row["low"]
        if c_range == 0:
            continue
        body = abs(row["close"] - row["open"])
        if (c_range - body) / c_range >= 0.70:
            return True
    return False


def _get_atr(df: pd.DataFrame) -> float | None:
    try:
        a = atr(df, 14)
        return float(a.iloc[-1]) if not a.empty else None
    except Exception:
        return None