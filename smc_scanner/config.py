# config.py — Central configuration for the SMC Scanner

# ── Exchange settings ──────────────────────────────────────────────────────────
EXCHANGE = "binance"          # CCXT exchange id
API_KEY  = ""                 # Leave empty for public (read-only) data
API_SECRET = ""

# ── Symbols & timeframes to scan ──────────────────────────────────────────────
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
]

TIMEFRAMES = ["15m", "1h"]   # Run analysis on each of these

# ── Candle history ─────────────────────────────────────────────────────────────
CANDLE_LIMIT = 200            # How many candles to fetch per request

# ── Structure detection ────────────────────────────────────────────────────────
SWING_LOOKBACK = 5            # Bars left/right to confirm a swing high/low
BOS_THRESHOLD  = 0.0010       # 0.10 % — minimum break size to call it a BOS

# ── Liquidity ─────────────────────────────────────────────────────────────────
EQH_EQL_TOLERANCE  = 0.0015  # 0.15 % — max delta to call two highs/lows "equal"
SWEEP_WICK_FACTOR  = 0.5     # Wick must cover >= 50 % of the candle range
SWEEP_LOOKBACK     = 30      # How many recent candles to look for sweep events

# ── Risk / reward ─────────────────────────────────────────────────────────────
DEFAULT_SL_BUFFER  = 0.002   # 0.20 % beyond the swept extreme for stop-loss
DEFAULT_RR_RATIO   = 2.0     # Minimum reward-to-risk ratio for TP

# ── Trend detection ───────────────────────────────────────────────────────────
TREND_EMA_FAST = 21
TREND_EMA_SLOW = 55

# ── Scanner loop ──────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 60   # Re-scan every N seconds (used in live mode)

# ── Output ────────────────────────────────────────────────────────────────────
SHOW_CHART = True             # Open plotly chart when a signal fires
LOG_FILE   = "signals.log"    # Append signals here; set "" to disable