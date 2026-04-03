# scoring.py — Signal quality scoring (0-100)
#
# Scoring table:
#   +40  valid setup baseline (sweep + BOS)
#   +15  RR >= MIN_RR_FOR_BONUS
#   +20  RR >= STRONG_RR_BONUS  (replaces +15)
#   +15  HTF aligned
#   -10  HTF opposing (only if HTF_FILTER_STRICT=False)
#   +10  volume spike
#   +10  clear sweep candle
#   +10  OB found and used for entry          ← NEW
#   + 8  OB has FVG overlap (extra precision) ← NEW
#   - 5  OB is mitigated (fallback entry)     ← NEW
#   -10  dead market (ATR very low)
#   -10  TP too close to entry (< 1 %)
#   -20  duplicate signal
#
# Score clamped to [0, 100].

import pandas as pd

import config
import journal
from utils import atr, log_info


def score_signal(sig: dict, df: pd.DataFrame) -> tuple[int, str]:
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

    # ── HTF confluence ────────────────────────────────────────────────────────
    htf_bias     = sig.get("htf_bias", "")
    htf_aligned  = sig.get("htf_aligned", False)
    htf_opposing = sig.get("htf_opposing", False)

    if htf_bias == "":
        reasons.append(" 0  HTF unavailable")
    elif htf_aligned:
        points += 15
        reasons.append(f"+15 HTF {sig.get('htf_tf','')} aligned ({htf_bias})")
    elif htf_opposing:
        points -= 10
        reasons.append(f"-10 HTF {sig.get('htf_tf','')} opposing ({htf_bias})")
    else:
        reasons.append(f" 0  HTF {sig.get('htf_tf','')} neutral ({htf_bias})")

    # ── Order Block quality ───────────────────────────────────────────────────
    ob_label   = sig.get("ob_label", "")
    ob_has_fvg = sig.get("ob_has_fvg", False)

    if ob_label:
        points += 10
        reasons.append(f"+10 OB entry ({ob_label})")
        if ob_has_fvg:
            points += 8
            reasons.append("+8  OB+FVG confluence (precise entry)")
    else:
        points -= 5
        reasons.append("-5  no OB — rough entry zone")

    # ── Volume confirmation ────────────────────────────────────────────────────
    if config.ENABLE_VOLUME_CONFIRMATION:
        if _volume_confirmed(df):
            points += 10
            reasons.append("+10 volume spike")
        else:
            reasons.append(" 0  no volume spike")

    # ── Sweep clarity ─────────────────────────────────────────────────────────
    if _sweep_is_clear(df):
        points += 10
        reasons.append("+10 clear sweep")
    else:
        reasons.append(" 0  sweep not crisp")

    # ── Volatility sanity ─────────────────────────────────────────────────────
    atr_val = _get_atr(df)
    if atr_val is not None and mid_entry > 0 and atr_val < mid_entry * 0.001:
        points -= 10
        reasons.append("-10 dead market")

    # ── TP distance ───────────────────────────────────────────────────────────
    tp_dist = abs(sig["tp"] - mid_entry) / mid_entry if mid_entry else 0
    if tp_dist < 0.01:
        points -= 10
        reasons.append("-10 TP too close (< 1 %)")

    # ── Deduplication ─────────────────────────────────────────────────────────
    recent = journal.get_recent_signals(
        sig["symbol"], sig["timeframe"], sig["direction"],
        config.DEDUP_LOOKBACK_HOURS,
    )
    if recent:
        points -= 20
        reasons.append(f"-20 duplicate ({len(recent)}x / {config.DEDUP_LOOKBACK_HOURS}h)")

    score = max(0, min(100, points))
    log_info(
        f"[SCORE] {sig['symbol']} {sig['timeframe']} {sig['direction'].upper()} "
        f"→ {score}/100  |  " + "  ".join(reasons)
    )
    return score, htf_bias


# ── Helpers ───────────────────────────────────────────────────────────────────

def _volume_confirmed(df: pd.DataFrame) -> bool:
    if "volume" not in df.columns or len(df) < config.VOLUME_LOOKBACK + 2:
        return False
    avg_vol   = df["volume"].iloc[-(config.VOLUME_LOOKBACK + 1):-1].mean()
    threshold = avg_vol * config.VOLUME_SPIKE_MULTIPLIER
    return (df["volume"].iloc[-1] >= threshold or
            df["volume"].iloc[-2] >= threshold)


def _sweep_is_clear(df: pd.DataFrame) -> bool:
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