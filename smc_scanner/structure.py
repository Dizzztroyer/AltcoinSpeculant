# structure.py — Swing detection, trend identification, BOS / MBOS logic

import numpy as np
import pandas as pd

import config
from utils import ema, pct_diff


# ── Swing high / low detection ─────────────────────────────────────────────────

def find_swings(df: pd.DataFrame, lookback: int | None = None) -> pd.DataFrame:
    """
    Add columns swing_high and swing_low to the DataFrame.

    A swing high at index i means df['high'][i] is the highest value in the
    window [i - lookback … i + lookback].
    A swing low  at index i means df['low'][i]  is the lowest  value in the
    same window.

    Both columns are boolean.
    """
    n = lookback or config.SWING_LOOKBACK
    df = df.copy()
    df["swing_high"] = False
    df["swing_low"]  = False

    for i in range(n, len(df) - n):
        window_highs = df["high"].iloc[i - n: i + n + 1]
        window_lows  = df["low"].iloc[i - n: i + n + 1]

        if df["high"].iloc[i] == window_highs.max():
            df.at[df.index[i], "swing_high"] = True

        if df["low"].iloc[i] == window_lows.min():
            df.at[df.index[i], "swing_low"] = True

    return df


def get_recent_swing_highs(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """Return the n most recent confirmed swing highs."""
    swings = df[df["swing_high"]].tail(n)
    return swings


def get_recent_swing_lows(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """Return the n most recent confirmed swing lows."""
    swings = df[df["swing_low"]].tail(n)
    return swings


# ── Market context / trend ─────────────────────────────────────────────────────

def get_market_context(df: pd.DataFrame) -> str:
    """
    Classify the market as 'bullish', 'bearish', or 'range'.

    Logic:
    1. Calculate fast EMA and slow EMA on close.
    2. If fast > slow  → potential bullish trend.
    3. If fast < slow  → potential bearish trend.
    4. Additionally check the structure of swing highs/lows.
       • HH + HL → bullish
       • LH + LL → bearish
       • mixed   → range
    """
    df = df.copy()
    df["ema_fast"] = ema(df["close"], config.TREND_EMA_FAST)
    df["ema_slow"] = ema(df["close"], config.TREND_EMA_SLOW)

    last = df.iloc[-1]
    ema_bias = "bullish" if last["ema_fast"] > last["ema_slow"] else "bearish"

    # Structural bias from last 4 swing highs / lows
    sh = df[df["swing_high"]].tail(4)["high"].values
    sl = df[df["swing_low"]].tail(4)["low"].values

    struct_bias = "range"
    if len(sh) >= 2 and len(sl) >= 2:
        hh = sh[-1] > sh[-2]   # higher high
        hl = sl[-1] > sl[-2]   # higher low
        lh = sh[-1] < sh[-2]   # lower high
        ll = sl[-1] < sl[-2]   # lower low

        if hh and hl:
            struct_bias = "bullish"
        elif lh and ll:
            struct_bias = "bearish"
        else:
            struct_bias = "range"

    # Combine: if both agree, use that; otherwise range
    if ema_bias == struct_bias:
        return struct_bias
    if struct_bias != "range":
        return struct_bias   # structural bias takes priority when defined
    return "range"


# ── Break of Structure (BOS) and Minor BOS (MBOS) ─────────────────────────────

class BOSEvent:
    """Represents a confirmed break of structure."""

    def __init__(self, direction: str, broken_level: float, candle_idx: int,
                 is_minor: bool = False):
        self.direction    = direction      # 'bullish' or 'bearish'
        self.broken_level = broken_level   # price level that was broken
        self.candle_idx   = candle_idx     # index in df where break occurred
        self.is_minor     = is_minor       # True → MBOS (minor / internal)

    def label(self) -> str:
        kind = "MBOS" if self.is_minor else "BOS"
        return f"{self.direction.upper()} {kind} @ {self.broken_level:.4f}"

    def __repr__(self):
        return f"<BOSEvent {self.label()} idx={self.candle_idx}>"


def detect_bos(df: pd.DataFrame) -> list[BOSEvent]:
    """
    Scan the DataFrame and return all BOS/MBOS events.

    Bullish BOS : close breaks ABOVE a recent swing high
    Bearish BOS : close breaks BELOW a recent swing low

    MBOS (minor): uses internal swing highs/lows (shorter lookback = 2)
    BOS  (major): uses full swing highs/lows (config.SWING_LOOKBACK)
    """
    events: list[BOSEvent] = []

    # Work on a copy with swings already marked
    df_full  = find_swings(df, lookback=config.SWING_LOOKBACK)
    df_minor = find_swings(df, lookback=2)

    threshold = config.BOS_THRESHOLD

    for is_minor, df_s in [(False, df_full), (True, df_minor)]:
        swing_highs = df_s[df_s["swing_high"]].copy()
        swing_lows  = df_s[df_s["swing_low"]].copy()

        for i in range(1, len(df_s)):
            close = df_s["close"].iloc[i]
            candle_idx = df_s.index[i]

            # Bullish BOS: close breaks above most recent swing high before i
            prev_sh = swing_highs[swing_highs.index < candle_idx]
            if not prev_sh.empty:
                level = prev_sh["high"].iloc[-1]
                if close > level * (1 + threshold):
                    # Avoid duplicates at same index
                    already = any(e.candle_idx == candle_idx and
                                  e.direction == "bullish" and
                                  e.is_minor == is_minor
                                  for e in events)
                    if not already:
                        events.append(BOSEvent("bullish", level, candle_idx, is_minor))

            # Bearish BOS: close breaks below most recent swing low before i
            prev_sl = swing_lows[swing_lows.index < candle_idx]
            if not prev_sl.empty:
                level = prev_sl["low"].iloc[-1]
                if close < level * (1 - threshold):
                    already = any(e.candle_idx == candle_idx and
                                  e.direction == "bearish" and
                                  e.is_minor == is_minor
                                  for e in events)
                    if not already:
                        events.append(BOSEvent("bearish", level, candle_idx, is_minor))

    return events


def get_last_bos(df: pd.DataFrame) -> BOSEvent | None:
    """Return the most recent BOS/MBOS event, or None."""
    events = detect_bos(df)
    if not events:
        return None
    return max(events, key=lambda e: e.candle_idx)


# ── Multi-timeframe confluence ─────────────────────────────────────────────────

class HTFConfluence:
    """
    Result of a higher-timeframe analysis for a given signal direction.

    Attributes
    ----------
    bias     : 'bullish' | 'bearish' | 'range'
    aligned  : True if HTF bias matches signal direction
    opposing : True if HTF bias directly opposes signal direction
    htf_tf   : which timeframe was consulted (e.g. '1h', '4h')
    reason   : short human-readable explanation
    """

    def __init__(self, bias: str, aligned: bool, opposing: bool,
                 htf_tf: str, reason: str):
        self.bias     = bias
        self.aligned  = aligned
        self.opposing = opposing
        self.htf_tf   = htf_tf
        self.reason   = reason

    def __repr__(self) -> str:
        flag = "ALIGNED" if self.aligned else ("OPPOSING" if self.opposing else "NEUTRAL")
        return f"<HTFConfluence {self.htf_tf} {self.bias.upper()} {flag}>"


def get_htf_confluence(symbol: str,
                       signal_direction: str,
                       signal_timeframe: str,
                       htf_map: dict | None = None) -> "HTFConfluence | None":
    """
    Fetch the higher timeframe for the given signal and evaluate alignment.

    Parameters
    ----------
    symbol            : e.g. 'BTC/USDT'
    signal_direction  : 'long' or 'short'
    signal_timeframe  : e.g. '15m'
    htf_map           : override config.HTF_MAP (optional)

    Returns
    -------
    HTFConfluence object, or None if HTF data is unavailable.

    Alignment rules
    ---------------
    • signal=long  + HTF=bullish → aligned   (green light)
    • signal=short + HTF=bearish → aligned   (green light)
    • signal=long  + HTF=bearish → opposing  (blocked when HTF_FILTER_STRICT=True)
    • signal=short + HTF=bullish → opposing  (blocked when HTF_FILTER_STRICT=True)
    • HTF=range                  → neutral   (no bonus, no block)
    """
    # Imported here to avoid circular imports at module level
    from datafeed import fetch_ohlcv

    mapping = htf_map or config.HTF_MAP
    htf_tf  = mapping.get(signal_timeframe)

    if not htf_tf:
        return None

    try:
        df_htf = fetch_ohlcv(symbol, htf_tf, limit=150)
        if df_htf.empty:
            return None
    except Exception:
        return None

    df_htf = find_swings(df_htf)
    bias   = get_market_context(df_htf)

    if signal_direction == "long":
        aligned  = bias == "bullish"
        opposing = bias == "bearish"
    else:
        aligned  = bias == "bearish"
        opposing = bias == "bullish"

    if aligned:
        reason = f"HTF {htf_tf} is {bias} — confirms {signal_direction} bias"
    elif opposing:
        reason = f"HTF {htf_tf} is {bias} — OPPOSES {signal_direction} signal"
    else:
        reason = f"HTF {htf_tf} is range — neutral"

    return HTFConfluence(
        bias=bias,
        aligned=aligned,
        opposing=opposing,
        htf_tf=htf_tf,
        reason=reason,
    )