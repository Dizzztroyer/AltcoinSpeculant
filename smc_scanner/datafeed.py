# datafeed.py — CCXT data fetching layer

import time

import ccxt
import pandas as pd

import config
from utils import log_info, log_warn, log_error


# ── Exchange singleton ─────────────────────────────────────────────────────────

def _build_exchange() -> ccxt.Exchange:
    exchange_class = getattr(ccxt, config.EXCHANGE)
    params = {"enableRateLimit": True}
    if config.API_KEY:
        params["apiKey"]  = config.API_KEY
        params["secret"]  = config.API_SECRET
    exchange = exchange_class(params)
    return exchange


_EXCHANGE: ccxt.Exchange | None = None


def get_exchange() -> ccxt.Exchange:
    global _EXCHANGE
    if _EXCHANGE is None:
        _EXCHANGE = _build_exchange()
        log_info(f"Connected to exchange: {config.EXCHANGE}")
    return _EXCHANGE


# ── OHLCV fetching ─────────────────────────────────────────────────────────────

def fetch_ohlcv(symbol: str, timeframe: str, limit: int | None = None) -> pd.DataFrame:
    """
    Fetch OHLCV candles from the exchange and return a clean DataFrame.

    Columns: timestamp, open, high, low, close, volume
    Index  : integer (reset)
    """
    ex = get_exchange()
    n  = limit or config.CANDLE_LIMIT

    try:
        raw = ex.fetch_ohlcv(symbol, timeframe, limit=n)
    except ccxt.NetworkError as e:
        log_warn(f"Network error fetching {symbol}/{timeframe}: {e}")
        return pd.DataFrame()
    except ccxt.ExchangeError as e:
        log_error(f"Exchange error fetching {symbol}/{timeframe}: {e}")
        return pd.DataFrame()

    if not raw:
        log_warn(f"No data returned for {symbol} {timeframe}")
        return pd.DataFrame()

    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Cast price/volume columns to float
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df


def fetch_all(symbols: list[str] | None = None,
              timeframes: list[str] | None = None) -> dict[tuple[str, str], pd.DataFrame]:
    """
    Fetch data for every (symbol, timeframe) combination.

    Returns a dict keyed by (symbol, timeframe).
    """
    symbols    = symbols    or config.SYMBOLS
    timeframes = timeframes or config.TIMEFRAMES
    data = {}

    for sym in symbols:
        for tf in timeframes:
            log_info(f"Fetching {sym} [{tf}] ...")
            df = fetch_ohlcv(sym, tf)
            if not df.empty:
                data[(sym, tf)] = df
            time.sleep(0.2)   # polite rate-limit pause

    return data