# liquidity.py — Liquidity zone mapping, equal high/low detection, sweep detection

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

import config
from structure import find_swings
from utils import pct_diff


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class LiquidityZone:
    """A price level where resting orders / stops are likely clustered."""
    level: float
    zone_type: Literal["high", "low", "equal_high", "equal_low"]
    candle_idx: int          # index of the candle that created this zone
    swept: bool = False      # True once price has taken this liquidity
    sweep_idx: int | None = None

    def label(self) -> str:
        return f"{self.zone_type.replace('_', ' ').title()} @ {self.level:.4f}"


@dataclass
class SweepEvent:
    """Records that a liquidity zone was swept by price."""
    zone: LiquidityZone
    candle_idx: int          # candle where the sweep occurred
    sweep_high: float        # candle's high
    sweep_low: float         # candle's low
    sweep_close: float       # candle's close
    direction: Literal["up", "down"]   # which way price spiked

    def label(self) -> str:
        side = "Above highs" if self.direction == "up" else "Below lows"
        return f"{side} ({self.zone.zone_type.replace('_', ' ')})"


# ── Liquidity zone builder ─────────────────────────────────────────────────────

def build_liquidity_zones(df: pd.DataFrame) -> list[LiquidityZone]:
    """
    Identify liquidity resting above swing highs and below swing lows,
    including equal highs and equal lows.

    Returns a list of LiquidityZone objects sorted by candle index.
    """
    df_s = find_swings(df)
    zones: list[LiquidityZone] = []

    swing_highs = df_s[df_s["swing_high"]].copy()
    swing_lows  = df_s[df_s["swing_low"]].copy()

    tol = config.EQH_EQL_TOLERANCE

    # ── Standard swing highs / lows ───────────────────────────────────────────
    for idx, row in swing_highs.iterrows():
        zones.append(LiquidityZone(
            level=row["high"],
            zone_type="high",
            candle_idx=idx,
        ))

    for idx, row in swing_lows.iterrows():
        zones.append(LiquidityZone(
            level=row["low"],
            zone_type="low",
            candle_idx=idx,
        ))

    # ── Equal highs ───────────────────────────────────────────────────────────
    highs = swing_highs["high"].values
    idxs  = swing_highs.index.tolist()
    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            if pct_diff(highs[i], highs[j]) <= tol:
                # Mark the later one as equal high
                avg = (highs[i] + highs[j]) / 2
                zones.append(LiquidityZone(
                    level=avg,
                    zone_type="equal_high",
                    candle_idx=idxs[j],
                ))

    # ── Equal lows ────────────────────────────────────────────────────────────
    lows = swing_lows["low"].values
    lidxs = swing_lows.index.tolist()
    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            if pct_diff(lows[i], lows[j]) <= tol:
                avg = (lows[i] + lows[j]) / 2
                zones.append(LiquidityZone(
                    level=avg,
                    zone_type="equal_low",
                    candle_idx=lidxs[j],
                ))

    # Remove duplicates (same level within tolerance, same type)
    unique: list[LiquidityZone] = []
    for z in zones:
        dup = any(
            z.zone_type == u.zone_type and pct_diff(z.level, u.level) < tol / 2
            for u in unique
        )
        if not dup:
            unique.append(z)

    return sorted(unique, key=lambda z: z.candle_idx)


# ── Sweep detector ─────────────────────────────────────────────────────────────

def detect_sweeps(df: pd.DataFrame,
                  zones: list[LiquidityZone] | None = None) -> list[SweepEvent]:
    """
    Detect liquidity sweep events in the recent candles.

    A sweep occurs when:
    • For a HIGH zone: a candle's wick pierces the level (high > level)
      but the candle closes BELOW the level → quick rejection.
    • For a LOW  zone: a candle's wick pierces the level (low < level)
      but the candle closes ABOVE the level → quick rejection.

    Only checks the last config.SWEEP_LOOKBACK candles.
    """
    if zones is None:
        zones = build_liquidity_zones(df)

    recent_df = df.tail(config.SWEEP_LOOKBACK).copy()
    sweeps: list[SweepEvent] = []
    wf = config.SWEEP_WICK_FACTOR

    for _, candle in recent_df.iterrows():
        c_idx   = candle.name
        c_high  = candle["high"]
        c_low   = candle["low"]
        c_close = candle["close"]
        c_open  = candle["open"]
        c_range = c_high - c_low if c_high != c_low else 1e-9

        for zone in zones:
            # Only look at zones BEFORE this candle
            if zone.candle_idx >= c_idx:
                continue
            if zone.swept:
                continue

            if zone.zone_type in ("high", "equal_high"):
                # Wick above the level, close back below
                wick_above = c_high - max(c_open, c_close)
                if c_high > zone.level and c_close < zone.level:
                    # Wick above must be meaningful
                    if wick_above / c_range >= wf * 0.5:
                        zone.swept     = True
                        zone.sweep_idx = c_idx
                        sweeps.append(SweepEvent(
                            zone=zone,
                            candle_idx=c_idx,
                            sweep_high=c_high,
                            sweep_low=c_low,
                            sweep_close=c_close,
                            direction="up",
                        ))

            elif zone.zone_type in ("low", "equal_low"):
                # Wick below the level, close back above
                wick_below = min(c_open, c_close) - c_low
                if c_low < zone.level and c_close > zone.level:
                    if wick_below / c_range >= wf * 0.5:
                        zone.swept     = True
                        zone.sweep_idx = c_idx
                        sweeps.append(SweepEvent(
                            zone=zone,
                            candle_idx=c_idx,
                            sweep_high=c_high,
                            sweep_low=c_low,
                            sweep_close=c_close,
                            direction="down",
                        ))

    # Sort chronologically
    sweeps.sort(key=lambda s: s.candle_idx)
    return sweeps


def get_last_sweep(df: pd.DataFrame) -> SweepEvent | None:
    """Return the most recent sweep event, or None."""
    zones  = build_liquidity_zones(df)
    sweeps = detect_sweeps(df, zones)
    return sweeps[-1] if sweeps else None