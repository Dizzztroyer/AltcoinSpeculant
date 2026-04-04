# scoring.py — Lightweight scoring layer (v3)
#
# The heavy lifting is now done by confirmation.py.
# scoring.py maps the CheckResult directly to a final score
# and adds only deduplication penalty on top.
#
# Final score = conf_score - dedup_penalty
# Threshold is still ALERT_SCORE_THRESHOLD (default 70).

import pandas as pd

import config
import journal
from utils import log_info


def score_signal(sig: dict, df: pd.DataFrame) -> tuple[int, str]:
    """
    Convert confirmation score into final signal score.

    Returns (score: int, htf_bias: str).
    """
    # Start from the confirmation engine's score
    score  = sig.get("conf_score", 0)
    passed = sig.get("conf_passed", [])
    failed = sig.get("conf_failed", [])

    reasons = [f"+{score} confirmation score"]
    reasons += [f"  ✅ {p}" for p in passed]
    reasons += [f"  ❌ {f}" for f in failed]

    # ── Kill zone bonus ──────────────────────────────────────────────────────
    kz_mode = getattr(config, "KILLZONE_MODE", "log")
    if kz_mode == "score":
        from killzones import KZResult, kz_score_bonus
        kz_quality = sig.get("kz_quality", 0)
        kz_in_zone = sig.get("kz_in_zone", True)
        kz_r = type("R", (), {"in_killzone": kz_in_zone, "zone_quality": kz_quality})()
        kz_pts = kz_score_bonus(kz_r)
        score += kz_pts
        if kz_pts != 0:
            reasons.append(f"{'+' if kz_pts>=0 else ''}{kz_pts} KZ ({sig.get('kz_zone_name','?')})")

    # ── Deduplication penalty ─────────────────────────────────────────────────
    recent = journal.get_recent_signals(
        sig["symbol"], sig["timeframe"], sig["direction"],
        config.DEDUP_LOOKBACK_HOURS,
    )
    if recent:
        score -= 15
        reasons.append(f"-15 duplicate ({len(recent)}x / {config.DEDUP_LOOKBACK_HOURS}h)")

    score = max(0, min(100, score))
    htf_bias = sig.get("htf_bias", "")

    log_info(
        f"[SCORE] {sig['symbol']} {sig['timeframe']} "
        f"{sig['direction'].upper()} → {score}/100"
    )
    return score, htf_bias


# ── Kept for alerts.py / main.py compatibility ────────────────────────────────

def _volume_confirmed(df: pd.DataFrame) -> bool:
    if "volume" not in df.columns or len(df) < config.VOLUME_LOOKBACK + 2:
        return False
    avg_vol   = df["volume"].iloc[-(config.VOLUME_LOOKBACK + 1):-1].mean()
    threshold = avg_vol * config.VOLUME_SPIKE_MULTIPLIER
    return (df["volume"].iloc[-1] >= threshold or
            df["volume"].iloc[-2] >= threshold)