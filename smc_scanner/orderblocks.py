# orderblocks.py — Order Block and Fair Value Gap detection
#
# ── Definitions ───────────────────────────────────────────────────────────────
#
# ORDER BLOCK (OB)
#   The last opposing candle immediately before an impulsive move that:
#     a) created a BOS, OR
#     b) swept a liquidity level
#
#   Bullish OB = last BEARISH candle before a bullish impulse
#     → price is expected to return to this candle and bounce UP
#     → entry_low  = OB low
#     → entry_high = OB high (= open of that bearish candle)
#
#   Bearish OB = last BULLISH candle before a bearish impulse
#     → price is expected to return to this candle and drop DOWN
#     → entry_low  = OB low (= open of that bullish candle)
#     → entry_high = OB high
#
# MITIGATION
#   An OB is "mitigated" (used up) once price trades back into it.
#   After mitigation it should not be used as entry zone again.
#
# FAIR VALUE GAP (FVG / Imbalance)
#   Three consecutive candles where:
#     Bullish FVG: candle[i-1].high  <  candle[i+1].low   (gap above)
#     Bearish FVG: candle[i-1].low   >  candle[i+1].high  (gap below)
#   The middle candle (i) is the impulse.
#   The gap itself = [candle[i-1].high, candle[i+1].low] for bullish
#
#   When an OB contains an FVG, that overlap is the highest-probability
#   entry zone.
#
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

import config
from utils import log_info


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class OrderBlock:
    """A single Order Block zone."""
    ob_type:    Literal["bullish", "bearish"]  # direction of expected reaction
    high:       float                           # top of the OB candle
    low:        float                           # bottom of the OB candle
    open:       float                           # open of the OB candle
    close:      float                           # close of the OB candle
    candle_idx: int                             # DataFrame index of OB candle
    impulse_idx: int                            # index of the impulse that confirmed it
    mitigated:  bool  = False                   # True once price re-enters

    # Optional FVG overlap refined zone
    fvg_high:   float | None = None
    fvg_low:    float | None = None

    @property
    def entry_high(self) -> float:
        """Upper boundary of the entry zone (prefer FVG if present)."""
        if self.fvg_high is not None and self.fvg_low is not None:
            return self.fvg_high
        # For bullish OB: entry is the full OB range
        # For bearish OB: entry is the full OB range
        return self.high

    @property
    def entry_low(self) -> float:
        """Lower boundary of the entry zone (prefer FVG if present)."""
        if self.fvg_high is not None and self.fvg_low is not None:
            return self.fvg_low
        return self.low

    @property
    def has_fvg(self) -> bool:
        return self.fvg_high is not None and self.fvg_low is not None

    def label(self) -> str:
        fvg = " +FVG" if self.has_fvg else ""
        return (f"{self.ob_type.title()} OB{fvg} "
                f"[{self.low:.2f} – {self.high:.2f}]")

    def __repr__(self) -> str:
        return f"<OrderBlock {self.label()} idx={self.candle_idx}>"


@dataclass
class FairValueGap:
    """A Fair Value Gap (imbalance) between three candles."""
    fvg_type:   Literal["bullish", "bearish"]
    gap_high:   float       # upper edge of the gap
    gap_low:    float       # lower edge of the gap
    candle_idx: int         # index of the MIDDLE (impulse) candle
    filled:     bool = False

    @property
    def midpoint(self) -> float:
        return (self.gap_high + self.gap_low) / 2

    def label(self) -> str:
        return (f"{self.fvg_type.title()} FVG "
                f"[{self.gap_low:.2f} – {self.gap_high:.2f}]")

    def __repr__(self) -> str:
        return f"<FVG {self.label()} idx={self.candle_idx}>"


# ── FVG detection ──────────────────────────────────────────────────────────────

def find_fvgs(df: pd.DataFrame,
              min_gap_pct: float | None = None) -> list[FairValueGap]:
    """
    Scan the entire DataFrame and return all Fair Value Gaps.

    Parameters
    ----------
    df          : OHLCV DataFrame (indexed 0..N)
    min_gap_pct : minimum gap size as fraction of price (default: OB_FVG_MIN_GAP)

    A gap must be at least min_gap_pct * mid_price wide to qualify,
    filtering out microstructure noise.
    """
    min_gap = min_gap_pct if min_gap_pct is not None else config.OB_FVG_MIN_GAP
    fvgs: list[FairValueGap] = []

    for i in range(1, len(df) - 1):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        nxt  = df.iloc[i + 1]
        mid  = curr["close"]

        # Bullish FVG: gap between prev candle high and next candle low
        if prev["high"] < nxt["low"]:
            gap_size = nxt["low"] - prev["high"]
            if gap_size / mid >= min_gap:
                fvgs.append(FairValueGap(
                    fvg_type="bullish",
                    gap_low=prev["high"],
                    gap_high=nxt["low"],
                    candle_idx=df.index[i],
                ))

        # Bearish FVG: gap between prev candle low and next candle high
        elif prev["low"] > nxt["high"]:
            gap_size = prev["low"] - nxt["high"]
            if gap_size / mid >= min_gap:
                fvgs.append(FairValueGap(
                    fvg_type="bearish",
                    gap_low=nxt["high"],
                    gap_high=prev["low"],
                    candle_idx=df.index[i],
                ))

    # Mark filled FVGs (price traded through them after formation)
    _mark_filled_fvgs(df, fvgs)
    return fvgs


def _mark_filled_fvgs(df: pd.DataFrame, fvgs: list[FairValueGap]) -> None:
    """Mark an FVG as filled once price trades back through it."""
    for fvg in fvgs:
        # Only check candles AFTER the FVG formed
        future = df[df.index > fvg.candle_idx]
        for _, row in future.iterrows():
            if fvg.fvg_type == "bullish" and row["low"] <= fvg.gap_low:
                fvg.filled = True
                break
            if fvg.fvg_type == "bearish" and row["high"] >= fvg.gap_high:
                fvg.filled = True
                break


# ── Order Block detection ──────────────────────────────────────────────────────

def find_order_blocks(df: pd.DataFrame,
                      fvgs: list[FairValueGap] | None = None,
                      lookback: int | None = None) -> list[OrderBlock]:
    """
    Detect Order Blocks in the DataFrame.

    Algorithm
    ---------
    For each candle i (the potential OB candle), look ahead up to
    OB_IMPULSE_LOOKFORWARD candles for an impulsive move:

    Bullish OB (last bearish candle before bullish impulse):
      • candle[i] is bearish  (close < open)
      • candle[i+1..i+k] has a bullish close that breaks above candle[i].high
        AND the move is at least OB_MIN_IMPULSE_PCT of candle[i] range
      • candle[i] is the LAST bearish candle before that impulse

    Bearish OB (last bullish candle before bearish impulse):
      • candle[i] is bullish  (close > open)
      • candle[i+1..i+k] has a bearish close that breaks below candle[i].low
      • candle[i] is the LAST bullish candle before that impulse

    After finding OBs:
      • check mitigation (price already returned into the OB)
      • attach best unfilled FVG that overlaps the OB

    Parameters
    ----------
    df       : OHLCV DataFrame
    fvgs     : pre-computed FVG list (computed if not provided)
    lookback : how many recent bars to search (default: OB_LOOKBACK)
    """
    if fvgs is None:
        fvgs = find_fvgs(df)

    lb       = lookback or config.OB_LOOKBACK
    forward  = config.OB_IMPULSE_LOOKFORWARD
    min_imp  = config.OB_MIN_IMPULSE_PCT

    # Work on the most recent `lb` candles only
    search_df = df.tail(lb).reset_index(drop=False)  # keep original index in 'index' col
    obs: list[OrderBlock] = []

    for pos in range(len(search_df) - 1):
        row      = search_df.iloc[pos]
        orig_idx = row["index"]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        is_bearish_candle = c < o
        is_bullish_candle = c > o

        # Look ahead for impulse
        end = min(pos + forward + 1, len(search_df))
        future_slice = search_df.iloc[pos + 1: end]

        # ── Potential Bullish OB ───────────────────────────────────────────────
        if is_bearish_candle:
            impulse_row = None
            for _, frow in future_slice.iterrows():
                # Impulse: close breaks above OB high
                if frow["close"] > h:
                    move = frow["close"] - h
                    if move / h >= min_imp:
                        impulse_row = frow
                    break
                # If another bearish candle appears before impulse → this is not the LAST bearish
                if frow["close"] < frow["open"]:
                    break

            if impulse_row is not None:
                ob = OrderBlock(
                    ob_type="bullish",
                    high=h, low=l, open=o, close=c,
                    candle_idx=orig_idx,
                    impulse_idx=int(impulse_row["index"]),
                )
                _check_mitigation(df, ob)
                _attach_fvg(ob, fvgs, df)
                obs.append(ob)

        # ── Potential Bearish OB ───────────────────────────────────────────────
        elif is_bullish_candle:
            impulse_row = None
            for _, frow in future_slice.iterrows():
                if frow["close"] < l:
                    move = l - frow["close"]
                    if move / l >= min_imp:
                        impulse_row = frow
                    break
                if frow["close"] > frow["open"]:
                    break

            if impulse_row is not None:
                ob = OrderBlock(
                    ob_type="bearish",
                    high=h, low=l, open=o, close=c,
                    candle_idx=orig_idx,
                    impulse_idx=int(impulse_row["index"]),
                )
                _check_mitigation(df, ob)
                _attach_fvg(ob, fvgs, df)
                obs.append(ob)

    return obs


def _check_mitigation(df: pd.DataFrame, ob: OrderBlock) -> None:
    """
    Mark OB as mitigated if price has already traded back into it
    after the impulse move.
    """
    future = df[df.index > ob.impulse_idx]
    for _, row in future.iterrows():
        if ob.ob_type == "bullish" and row["low"] <= ob.high:
            ob.mitigated = True
            return
        if ob.ob_type == "bearish" and row["high"] >= ob.low:
            ob.mitigated = True
            return


def _attach_fvg(ob: OrderBlock,
                fvgs: list[FairValueGap],
                df: pd.DataFrame) -> None:
    """
    Find the best unfilled FVG that overlaps with this OB and is
    between the OB candle and the current price.
    Attach its boundaries as the refined entry zone.
    """
    fvg_type = ob.ob_type  # matching FVG type

    best: FairValueGap | None = None
    best_size = 0.0

    for fvg in fvgs:
        if fvg.fvg_type != fvg_type:
            continue
        if fvg.filled:
            continue
        # FVG must be between OB candle and now
        if fvg.candle_idx <= ob.candle_idx:
            continue

        # Check overlap with OB range
        overlap_low  = max(fvg.gap_low,  ob.low)
        overlap_high = min(fvg.gap_high, ob.high)

        if overlap_high > overlap_low:
            size = overlap_high - overlap_low
            if size > best_size:
                best_size = size
                best = fvg

    # If no overlap, look for FVG just above/below OB within OB_FVG_SEARCH_RANGE
    if best is None:
        search_range = (ob.high - ob.low) * config.OB_FVG_SEARCH_RANGE
        for fvg in fvgs:
            if fvg.fvg_type != fvg_type:
                continue
            if fvg.filled:
                continue
            if fvg.candle_idx <= ob.candle_idx:
                continue
            if ob.ob_type == "bullish":
                # FVG just above OB low
                if ob.low - search_range <= fvg.gap_low <= ob.high + search_range:
                    size = fvg.gap_high - fvg.gap_low
                    if size > best_size:
                        best_size = size
                        best = fvg
            else:
                if ob.low - search_range <= fvg.gap_high <= ob.high + search_range:
                    size = fvg.gap_high - fvg.gap_low
                    if size > best_size:
                        best_size = size
                        best = fvg

    if best is not None:
        ob.fvg_high = best.gap_high
        ob.fvg_low  = best.gap_low


# ── Public helpers ─────────────────────────────────────────────────────────────

def get_nearest_ob(obs: list[OrderBlock],
                   direction: str,
                   current_price: float,
                   mitigated_ok: bool = False) -> OrderBlock | None:
    """
    Return the most relevant unmitigated OB for the given trade direction.

    For LONG  → bullish OB below current price (price not yet returned)
    For SHORT → bearish OB above current price
    """
    ob_type = "bullish" if direction == "long" else "bearish"
    candidates = [
        ob for ob in obs
        if ob.ob_type == ob_type
        and (mitigated_ok or not ob.mitigated)
    ]

    if not candidates:
        return None

    if direction == "long":
        # Want OB below current price, closest to it
        below = [ob for ob in candidates if ob.high <= current_price * 1.005]
        if not below:
            return None
        return max(below, key=lambda ob: ob.high)   # highest = closest

    else:
        # Want OB above current price
        above = [ob for ob in candidates if ob.low >= current_price * 0.995]
        if not above:
            return None
        return min(above, key=lambda ob: ob.low)    # lowest = closest