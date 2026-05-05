# datafeed.py - CCXT data fetching layer

import time
from datetime import datetime, timezone, timedelta

import ccxt
import pandas as pd

import config
from utils import log_info, log_warn, log_error


# Exchange singleton

def _build_exchange():
    exchange_class = getattr(ccxt, config.EXCHANGE)
    params = {"enableRateLimit": True}
    if config.API_KEY:
        params["apiKey"]  = config.API_KEY
        params["secret"]  = config.API_SECRET
    exchange = exchange_class(params)
    return exchange


_EXCHANGE = None


def get_exchange():
    global _EXCHANGE
    if _EXCHANGE is None:
        _EXCHANGE = _build_exchange()
        log_info(f"Connected to exchange: {config.EXCHANGE}")
    return _EXCHANGE


# Single-request OHLCV (live scanning, max ~1000 candles)

def fetch_ohlcv(symbol, timeframe, limit=None):
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
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# Paginated historical fetch (backtesting - bypasses 1000-candle limit)

_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480,
    "12h": 720, "1d": 1440, "3d": 4320, "1w": 10080,
}


def fetch_full_history(symbol, timeframe, days):
    tf_min = _TF_MINUTES.get(timeframe)
    if tf_min is None:
        log_warn(f"[datafeed] Unknown timeframe '{timeframe}', fallback to fetch_ohlcv")
        return fetch_ohlcv(symbol, timeframe)

    total_needed = int(days * 1440 / tf_min) + 200
    batch_size   = 1000
    max_batches  = (total_needed // batch_size) + 5

    since_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=days + 1)).timestamp() * 1000
    )

    ex        = get_exchange()
    all_rows  = []
    fetched   = 0
    cursor_ms = since_ms

    log_info(
        f"[datafeed] Paginated fetch {symbol} [{timeframe}] "
        f"~{total_needed} candles over {days}d (up to {max_batches} batches)"
    )

    for batch_num in range(1, max_batches + 1):
        try:
            raw = ex.fetch_ohlcv(symbol, timeframe, since=cursor_ms, limit=batch_size)
        except ccxt.NetworkError as e:
            log_warn(f"[datafeed] Network error batch {batch_num}: {e} - retrying")
            time.sleep(2)
            try:
                raw = ex.fetch_ohlcv(symbol, timeframe, since=cursor_ms, limit=batch_size)
            except Exception as e2:
                log_error(f"[datafeed] Second failure batch {batch_num}: {e2}")
                break
        except ccxt.ExchangeError as e:
            log_error(f"[datafeed] Exchange error batch {batch_num}: {e}")
            break

        if not raw:
            log_info(f"[datafeed]   batch {batch_num}: no data, stopping")
            break

        all_rows.extend(raw)
        fetched   += len(raw)
        cursor_ms  = raw[-1][0] + 1

        last_dt = datetime.fromtimestamp(raw[-1][0] / 1000, tz=timezone.utc)
        log_info(
            f"[datafeed]   batch {batch_num:>3}: "
            f"+{len(raw):>4} candles (total {fetched:>5}) "
            f"- last {last_dt.strftime('%Y-%m-%d %H:%M')}"
        )

        if cursor_ms >= datetime.now(timezone.utc).timestamp() * 1000:
            break

        time.sleep(0.25)

    if not all_rows:
        log_warn(f"[datafeed] fetch_full_history: no data for {symbol} [{timeframe}]")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    log_info(
        f"[datafeed] fetch_full_history done: {len(df)} candles  "
        f"{df['timestamp'].iloc[0].strftime('%Y-%m-%d')} -> "
        f"{df['timestamp'].iloc[-1].strftime('%Y-%m-%d')}"
    )
    return df


# Fetch all symbol/timeframe combos (live scanning)

def fetch_all(symbols=None, timeframes=None):
    symbols    = symbols    or config.SYMBOLS
    timeframes = timeframes or config.TIMEFRAMES
    data = {}
    for sym in symbols:
        for tf in timeframes:
            log_info(f"Fetching {sym} [{tf}] ...")
            df = fetch_ohlcv(sym, tf)
            if not df.empty:
                data[(sym, tf)] = df
            time.sleep(0.2)
    return data
