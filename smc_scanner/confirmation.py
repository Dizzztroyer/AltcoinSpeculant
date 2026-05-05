# confirmation.py — Multi-layer SMC confirmation engine
#
# This module is the heart of the A+ filter system.
# It evaluates every candidate trade through 7 strict layers
# and produces a detailed CheckResult with score, passed/failed
# confirmations, and explicit rejection reasons.
#
# ── Layer weights (total = 100) ───────────────────────────────────────────────
#   HTF alignment        25 pts  MANDATORY — hard block if fails
#   Sweep quality        15 pts  MANDATORY — hard block if fails
#   BOS strength         20 pts  MANDATORY — hard block if fails
#   OB / FVG presence    20 pts  MANDATORY — hard block if fails
#   Premium/Discount      10 pts  hard block if fails
#   Liquidity target      10 pts  hard block if fails
#   ─────────────────────────────────────────────────
#   Maximum              100 pts
#   Minimum to trade      ≥ CONFIRMATION_MIN_SCORE (default 70)
#
# Hard blocks: if a MANDATORY layer fails, signal is dropped entirely
# regardless of score in other layers.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

import config
from orderblocks import FairValueGap, OrderBlock, find_fvgs, find_order_blocks
from structure import (find_swings, get_market_context,
                       get_htf_confluence, get_deep_htf_confluence)
from liquidity import SweepEvent, LiquidityZone
from utils import log_info, atr
from patterns import analyse_patterns, PatternContext


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """
    Full confirmation report for one candidate trade.

    allowed      : True only if all mandatory layers pass AND score >= threshold
    score        : 0-100 weighted score
    passed       : list of confirmation descriptions that passed
    failed       : list of confirmation descriptions that failed (with reason)
    rejected_by  : name of the layer that hard-blocked the trade (or "")
    htf_bias     : 'bullish' | 'bearish' | 'range' | ''
    htf_tf       : which HTF was checked
    pd_zone      : 'premium' | 'discount' | 'equilibrium'
    liq_target   : price of the identified liquidity target (or None)
    sweep_score  : 0-15 sub-score for sweep quality
    bos_score    : 0-20 sub-score for BOS strength
    ob_score     : 0-20 sub-score for OB/FVG quality
    """
    allowed:      bool  = False
    score:        int   = 0
    passed:       list[str] = field(default_factory=list)
    failed:       list[str] = field(default_factory=list)
    rejected_by:  str   = ""
    htf_bias:     str   = ""
    htf_tf:       str   = ""
    pd_zone:      str   = ""
    liq_target:   float | None = None
    sweep_score:  int   = 0
    bos_score:    int   = 0
    ob_score:     int   = 0
    pattern_ctx:  object | None = None   # PatternContext (patterns.py)

    def summary(self) -> str:
        status = "✅ ALLOWED" if self.allowed else f"❌ BLOCKED ({self.rejected_by})"
        return (f"{status}  score={self.score}/100  "
                f"passed={len(self.passed)}  failed={len(self.failed)}")


# ── Public entry point ─────────────────────────────────────────────────────────

def run_confirmations(
    symbol:    str,
    timeframe: str,
    direction: str,          # 'long' | 'short'
    df:        pd.DataFrame, # LTF OHLCV with swings already applied
    sweep:     SweepEvent,
    bos_candle_idx: int,     # index of BOS candle
    bos_level: float,        # price level broken
    obs:       list[OrderBlock],
    fvgs:      list[FairValueGap],
    zones:     list[LiquidityZone],
) -> CheckResult:
    """
    Run all 6 confirmation layers in order.
    Returns a CheckResult with full details.
    """
    r = CheckResult()
    total = 0

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 1 — HTF ALIGNMENT (25 pts, MANDATORY)
    # ─────────────────────────────────────────────────────────────────────────
    htf_pts, htf_bias, htf_tf = _check_htf(symbol, timeframe, direction)
    r.htf_bias = htf_bias
    r.htf_tf   = htf_tf

    if htf_pts == 0 and config.CONFIRMATION_HTF_MANDATORY:
        r.rejected_by = "HTF"
        r.failed.append(f"HTF MANDATORY FAIL: {htf_tf} bias={htf_bias} opposes {direction}")
        _finalise(r, total)
        return r

    total += htf_pts
    if htf_pts > 0:
        r.passed.append(f"HTF {htf_tf} {htf_bias} aligned (+{htf_pts})")
    else:
        r.failed.append(f"HTF {htf_tf} {htf_bias} — neutral (0 pts)")

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 2 — SWEEP QUALITY (15 pts, MANDATORY)
    # ─────────────────────────────────────────────────────────────────────────
    sweep_pts, sweep_detail = _check_sweep_quality(sweep, df)
    r.sweep_score = sweep_pts

    if sweep_pts == 0 and config.CONFIRMATION_SWEEP_MANDATORY:
        r.rejected_by = "SWEEP"
        r.failed.append(f"SWEEP MANDATORY FAIL: {sweep_detail}")
        _finalise(r, total)
        return r

    total += sweep_pts
    if sweep_pts > 0:
        r.passed.append(f"Sweep quality +{sweep_pts}: {sweep_detail}")
    else:
        r.failed.append(f"Sweep weak: {sweep_detail}")

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 3 — BOS STRENGTH (20 pts, MANDATORY)
    # ─────────────────────────────────────────────────────────────────────────
    bos_pts, bos_detail = _check_bos_strength(df, bos_candle_idx, bos_level,
                                               direction, fvgs)
    r.bos_score = bos_pts

    if bos_pts == 0 and config.CONFIRMATION_BOS_MANDATORY:
        r.rejected_by = "BOS"
        r.failed.append(f"BOS MANDATORY FAIL: {bos_detail}")
        _finalise(r, total)
        return r

    total += bos_pts
    if bos_pts > 0:
        r.passed.append(f"BOS strength +{bos_pts}: {bos_detail}")
    else:
        r.failed.append(f"BOS weak: {bos_detail}")

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 4 — OB / FVG PRESENCE (20 pts, MANDATORY)
    # ─────────────────────────────────────────────────────────────────────────
    ob_pts, ob_detail = _check_ob_fvg(obs, fvgs, direction, df)
    r.ob_score = ob_pts

    if ob_pts == 0 and config.CONFIRMATION_OB_MANDATORY:
        r.rejected_by = "OB/FVG"
        r.failed.append(f"OB/FVG MANDATORY FAIL: no valid OB or FVG found")
        _finalise(r, total)
        return r

    total += ob_pts
    if ob_pts > 0:
        r.passed.append(f"OB/FVG +{ob_pts}: {ob_detail}")
    else:
        r.failed.append(f"No OB/FVG: {ob_detail}")

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 5 — PREMIUM / DISCOUNT ZONE (10 pts)
    # ─────────────────────────────────────────────────────────────────────────
    pd_pts, pd_zone, pd_detail = _check_premium_discount(df, direction)
    r.pd_zone = pd_zone

    if pd_pts == 0 and config.CONFIRMATION_PD_MANDATORY:
        r.rejected_by = "PREMIUM/DISCOUNT"
        r.failed.append(f"P/D MANDATORY FAIL: {direction} in {pd_zone} zone — {pd_detail}")
        _finalise(r, total)
        return r

    total += pd_pts
    if pd_pts > 0:
        r.passed.append(f"P/D zone +{pd_pts}: {pd_zone} ({pd_detail})")
    else:
        r.failed.append(f"P/D zone: {direction} in {pd_zone} — unfavourable ({pd_detail})")

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 6 — LIQUIDITY TARGET (10 pts)
    # ─────────────────────────────────────────────────────────────────────────
    liq_pts, liq_target, liq_detail = _check_liquidity_target(
        df, direction, zones, sweep)
    r.liq_target = liq_target

    if liq_pts == 0 and config.CONFIRMATION_LIQ_TARGET_MANDATORY:
        r.rejected_by = "LIQ_TARGET"
        r.failed.append(f"LIQ TARGET MANDATORY FAIL: {liq_detail}")
        _finalise(r, total)
        return r

    total += liq_pts
    if liq_pts > 0:
        r.passed.append(f"Liq target +{liq_pts}: {liq_detail}")
    else:
        r.failed.append(f"No clear liq target: {liq_detail}")

    # ─────────────────────────────────────────────────────────────────────────
    # FINAL SCORE CHECK
    # ─────────────────────────────────────────────────────────────────────────
    _finalise(r, total)

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 7 — INSTITUTIONAL PATTERNS (QM / Fakeout / SR-flip / CP / MPL)
    # Runs only when the signal has already passed layers 1-6.
    # Can add/subtract score and hard-block fakeout setups.
    # ─────────────────────────────────────────────────────────────────────────
    try:
        from liquidity import SweepEvent as _SE
        if sweep is not None:
            sweep_level = sweep.zone.level
            sweep_idx   = sweep.candle_idx
        else:
            sweep_level = bos_level
            sweep_idx   = bos_candle_idx

        pat_ctx = analyse_patterns(
            df=df, direction=direction,
            sweep_candle_idx=sweep_idx,
            sweep_level=sweep_level,
            bos_candle_idx=bos_candle_idx,
            bos_level=bos_level,
        )
        r.pattern_ctx = pat_ctx

        # Hard block (fakeout with high confidence)
        if pat_ctx.is_hard_blocked:
            r.allowed     = False
            r.rejected_by = (f"FAKEOUT/{pat_ctx.fakeout.fakeout_type} "
                             f"(conf={pat_ctx.fakeout.confidence:.0%})")
            r.failed.append(f"Pattern hard-block: {pat_ctx.fakeout.description}")
            return r

        # Score adjustment
        adj = pat_ctx.net_score_adjustment
        if adj != 0:
            r.score = max(0, min(100, r.score + adj))
            for ln in pat_ctx.summary_lines():
                if adj > 0:
                    r.passed.append(f"Pattern: {ln}")
                else:
                    r.failed.append(f"Pattern: {ln}")
    except Exception as _pat_exc:
        log_info(f"[PATTERN] error (non-fatal): {_pat_exc}")

    # Re-check score threshold after pattern adjustment
    if r.score < config.CONFIRMATION_MIN_SCORE:
        r.allowed     = False
        r.rejected_by = f"SCORE ({r.score} < {config.CONFIRMATION_MIN_SCORE})"

    return r


# ── Layer implementations ──────────────────────────────────────────────────────

def _check_htf(symbol: str, timeframe: str,
               direction: str) -> tuple[int, str, str]:
    """
    HTF alignment check.

    When DEEP_HTF_ENABLED=True (default): checks 2-3 HTF layers
    (e.g. 4h + 1d + 1w for a 1h signal) and scores each proportionally.
    Provides richer context than a single HTF level.

    When DEEP_HTF_ENABLED=False: falls back to the original single-layer check.

    Returns (points, primary_bias, htf_tf_description).
    """
    use_deep = getattr(config, "DEEP_HTF_ENABLED", True)

    if use_deep:
        result = get_deep_htf_confluence(symbol, direction, timeframe)

        # hard_opposing = any layer directly opposes → treat as 0 pts
        # (mandatory block will fire if CONFIRMATION_HTF_MANDATORY=True)
        if result.hard_opposing and len(result.aligned_layers) == 0:
            # All opposing, none aligned
            primary_bias = result.opposing_layers[0][1] if result.opposing_layers else "opposing"
            htf_desc = result.summary[:60]
            return 0, primary_bias, htf_desc

        # Partial opposing: reduce score but don't fully block
        # (hard block only when ALL layers oppose, handled above)
        primary_bias = (result.aligned_layers[0][1]  if result.aligned_layers
                        else result.neutral_layers[0][1] if result.neutral_layers
                        else "range")
        htf_desc = result.summary[:80]
        return result.total_pts, primary_bias, htf_desc

    else:
        # Original single-layer logic
        confluence = get_htf_confluence(symbol, direction, timeframe)
        if confluence is None:
            return 0, "unavailable", ""
        if confluence.aligned:
            return 25, confluence.bias, confluence.htf_tf
        if confluence.opposing:
            return 0, confluence.bias, confluence.htf_tf
        return 0, confluence.bias, confluence.htf_tf


def _check_sweep_quality(sweep: SweepEvent,
                          df: pd.DataFrame) -> tuple[int, str]:
    """
    Score the sweep on 3 criteria (max 15 pts):
      - Wick dominance: wick >= 60% of candle range         → 5 pts
      - Fast rejection: next 1-2 candles move opposite      → 5 pts
      - Strong level:  zone is equal H/L or major swing     → 5 pts
    """
    pts = 0
    details = []

    # Locate sweep candle in df
    if sweep.candle_idx not in df.index:
        return 0, "sweep candle not in dataframe"

    candle = df.loc[sweep.candle_idx]
    c_range = candle["high"] - candle["low"]
    if c_range == 0:
        return 0, "zero-range candle"

    body = abs(candle["close"] - candle["open"])
    wick = c_range - body

    # ── Wick dominance ────────────────────────────────────────────────────────
    wick_ratio = wick / c_range
    if wick_ratio >= config.SWEEP_WICK_DOMINANCE:
        pts += 5
        details.append(f"wick {wick_ratio:.0%}")
    else:
        details.append(f"wick {wick_ratio:.0%} (weak)")

    # ── Fast rejection: check 2 candles after sweep ───────────────────────────
    future = df[df.index > sweep.candle_idx].head(2)
    if not future.empty:
        if sweep.direction == "up":
            # After sweeping highs, next candles should move DOWN
            rejection = future["close"].iloc[-1] < candle["close"]
            move = (candle["close"] - future["close"].iloc[-1]) / candle["close"]
        else:
            # After sweeping lows, next candles should move UP
            rejection = future["close"].iloc[-1] > candle["close"]
            move = (future["close"].iloc[-1] - candle["close"]) / candle["close"]

        if rejection and move >= 0.001:  # at least 0.1% move
            pts += 5
            details.append(f"fast rejection {move:.2%}")
        else:
            details.append("no fast rejection")
    else:
        details.append("no candles after sweep")

    # ── Strong level ──────────────────────────────────────────────────────────
    zone_type = sweep.zone.zone_type
    if zone_type in ("equal_high", "equal_low"):
        pts += 5
        details.append("equal H/L level")
    elif zone_type in ("high", "low"):
        pts += 3
        details.append("swing H/L level")

    return pts, " | ".join(details)


def _check_bos_strength(df: pd.DataFrame,
                         bos_idx: int,
                         bos_level: float,
                         direction: str,
                         fvgs: list[FairValueGap]) -> tuple[int, str]:
    """
    Score BOS quality (max 20 pts):
      - Large displacement candle: body >= ATR × factor     → 8 pts
      - Close beyond structure (not just wick)              → 6 pts
      - FVG formed on the BOS candle                        → 6 pts
    """
    pts = 0
    details = []

    if bos_idx not in df.index:
        return 0, "BOS candle not in dataframe"

    candle = df.loc[bos_idx]
    body   = abs(candle["close"] - candle["open"])
    c_range = candle["high"] - candle["low"]

    # ── Displacement: big body relative to ATR ────────────────────────────────
    atr_val = _rolling_atr(df, bos_idx)
    if atr_val and atr_val > 0:
        body_ratio = body / atr_val
        if body_ratio >= config.BOS_MIN_BODY_ATR_RATIO:
            pts += 8
            details.append(f"strong displacement {body_ratio:.1f}×ATR")
        elif body_ratio >= config.BOS_MIN_BODY_ATR_RATIO * 0.6:
            pts += 4
            details.append(f"moderate displacement {body_ratio:.1f}×ATR")
        else:
            details.append(f"weak body {body_ratio:.1f}×ATR")
    else:
        details.append("ATR unavailable")

    # ── Close beyond structure (close, not just wick) ─────────────────────────
    if direction == "long" and candle["close"] > bos_level:
        pts += 6
        details.append("close above structure")
    elif direction == "short" and candle["close"] < bos_level:
        pts += 6
        details.append("close below structure")
    else:
        details.append("wick-only BOS (body didn't close beyond)")

    # ── FVG formed during BOS impulse ────────────────────────────────────────
    # FVG candle index is the MIDDLE candle — so check idx-1, idx, idx+1
    fvg_on_bos = any(
        abs(fvg.candle_idx - bos_idx) <= 1 and
        fvg.fvg_type == ("bullish" if direction == "long" else "bearish")
        for fvg in fvgs
    )
    if fvg_on_bos:
        pts += 6
        details.append("FVG formed on BOS impulse")
    else:
        details.append("no FVG on BOS")

    return pts, " | ".join(details)


def _check_ob_fvg(obs: list[OrderBlock],
                   fvgs: list[FairValueGap],
                   direction: str,
                   df: pd.DataFrame) -> tuple[int, str]:
    """
    Check for a valid unmitigated OB or unfilled FVG (max 20 pts).
      - OB present and unmitigated                          → 12 pts
      - OB has FVG overlap                                  →  8 pts additional
      - Only FVG (no OB)                                    →  8 pts
    At least one must exist → else 0.
    """
    ob_type  = "bullish" if direction == "long" else "bearish"
    fvg_type = "bullish" if direction == "long" else "bearish"

    current_price = df["close"].iloc[-1]

    # Find best unmitigated OB in the right direction
    candidates = [
        ob for ob in obs
        if ob.ob_type == ob_type and not ob.mitigated
    ]

    # Must be below current price for long, above for short
    if direction == "long":
        candidates = [ob for ob in candidates if ob.high <= current_price * 1.01]
    else:
        candidates = [ob for ob in candidates if ob.low >= current_price * 0.99]

    best_ob = (max(candidates, key=lambda ob: ob.high)
               if direction == "long" and candidates else
               min(candidates, key=lambda ob: ob.low)
               if direction == "short" and candidates else None)

    # Find best unfilled FVG
    fvg_candidates = [
        fvg for fvg in fvgs
        if fvg.fvg_type == fvg_type and not fvg.filled
    ]

    has_fvg = len(fvg_candidates) > 0

    if best_ob and best_ob.has_fvg:
        return 20, f"OB+FVG confluence at {best_ob.low:.2f}–{best_ob.high:.2f}"
    elif best_ob:
        pts = 12 + (8 if has_fvg else 0)
        fvg_note = " (+FVG nearby)" if has_fvg else ""
        return pts, f"OB at {best_ob.low:.2f}–{best_ob.high:.2f}{fvg_note}"
    elif has_fvg:
        fvg = fvg_candidates[-1]
        return 8, f"FVG only at {fvg.gap_low:.2f}–{fvg.gap_high:.2f}"
    else:
        return 0, "no OB or FVG found"


def _check_premium_discount(df: pd.DataFrame,
                              direction: str) -> tuple[int, str, str]:
    """
    Determine whether current price is in premium or discount zone (max 10 pts).

    Range = highest high to lowest low over OB_LOOKBACK candles.
    Equilibrium = 50% of the range.
    Discount = lower 50% (good for longs).
    Premium  = upper 50% (good for shorts).

    Returns (points, zone_name, detail).
    """
    lookback = min(config.OB_LOOKBACK, len(df))
    window   = df.tail(lookback)

    range_high = window["high"].max()
    range_low  = window["low"].min()
    mid        = (range_high + range_low) / 2
    current    = df["close"].iloc[-1]

    rng = range_high - range_low
    if rng == 0:
        return 5, "equilibrium", "zero range"

    position_pct = (current - range_low) / rng  # 0 = bottom, 1 = top

    if position_pct <= 0.35:
        zone = "discount"
    elif position_pct >= 0.65:
        zone = "premium"
    else:
        zone = "equilibrium"

    detail = f"price at {position_pct:.0%} of range [{range_low:.2f}–{range_high:.2f}]"

    if direction == "long" and zone == "discount":
        return 10, zone, detail
    elif direction == "short" and zone == "premium":
        return 10, zone, detail
    elif zone == "equilibrium":
        return 5, zone, detail   # neutral — partial credit
    else:
        return 0, zone, detail   # wrong side


def _check_liquidity_target(df: pd.DataFrame,
                              direction: str,
                              zones: list[LiquidityZone],
                              sweep: SweepEvent) -> tuple[int, float | None, str]:
    """
    Find a clear liquidity target in the trade direction (max 10 pts).

    LONG  → look for an unswept HIGH or EQUAL HIGH above current price
    SHORT → look for an unswept LOW  or EQUAL LOW  below current price

    Target must be at least MIN_TARGET_DISTANCE away.
    """
    current = df["close"].iloc[-1]
    min_dist = config.CONFIRMATION_MIN_TARGET_DISTANCE

    if direction == "long":
        # Look for highs above current price that haven't been swept
        targets = [
            z for z in zones
            if z.zone_type in ("high", "equal_high")
            and z.level > current * (1 + min_dist)
            and not z.swept
        ]
        if targets:
            nearest = min(targets, key=lambda z: z.level)
            dist = (nearest.level - current) / current
            return 10, nearest.level, f"{nearest.label()} dist={dist:.2%}"
    else:
        # Look for lows below current price
        targets = [
            z for z in zones
            if z.zone_type in ("low", "equal_low")
            and z.level < current * (1 - min_dist)
            and not z.swept
        ]
        if targets:
            nearest = max(targets, key=lambda z: z.level)
            dist = (current - nearest.level) / current
            return 10, nearest.level, f"{nearest.label()} dist={dist:.2%}"

    return 0, None, f"no unswept {'highs' if direction == 'long' else 'lows'} found"


# ── Internal helpers ───────────────────────────────────────────────────────────

def _rolling_atr(df: pd.DataFrame, at_idx: int, period: int = 14) -> float | None:
    """ATR value at a specific candle index."""
    try:
        pos = df.index.get_loc(at_idx)
        if pos < period:
            return None
        window = df.iloc[max(0, pos - period): pos + 1]
        tr = pd.concat([
            window["high"] - window["low"],
            (window["high"] - window["close"].shift(1)).abs(),
            (window["low"]  - window["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        return float(tr.mean())
    except Exception:
        return None


def _finalise(r: CheckResult, raw_score: int) -> None:
    r.score   = max(0, min(100, raw_score))
    r.allowed = (r.rejected_by == "" and
                 r.score >= config.CONFIRMATION_MIN_SCORE)