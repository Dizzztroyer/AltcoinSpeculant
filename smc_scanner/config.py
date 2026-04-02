# config.py — SMC Scanner v2 — Central configuration
# ─────────────────────────────────────────────────────────────────────────────

# ── A. Exchange settings ──────────────────────────────────────────────────────
EXCHANGE   = "binance"
API_KEY    = ""
API_SECRET = ""

# ── B. Scanner settings ───────────────────────────────────────────────────────
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
]

TIMEFRAMES = ["15m", "1h"]

# Higher-timeframe map used by scoring
HTF_MAP = {
    "1m":  "15m",
    "5m":  "15m",
    "15m": "1h",
    "30m": "4h",
    "1h":  "4h",
    "4h":  "1d",
    "1d":  "1w",
}

CANDLE_LIMIT          = 200
SCAN_INTERVAL_SECONDS = 3600

SWING_LOOKBACK    = 5
BOS_THRESHOLD     = 0.0010
EQH_EQL_TOLERANCE = 0.0015
SWEEP_WICK_FACTOR = 0.5
SWEEP_LOOKBACK    = 30
DEFAULT_SL_BUFFER = 0.002
DEFAULT_RR_RATIO  = 2.0
TREND_EMA_FAST    = 21
TREND_EMA_SLOW    = 55

# ── C. Database settings ──────────────────────────────────────────────────────
DB_PATH = "signals.db"

# ── D. Evaluation settings ────────────────────────────────────────────────────
SIGNAL_EXPIRY_HOURS       = 48
EVALUATION_LOOKAHEAD_BARS = 100

# ── E. Scoring settings ───────────────────────────────────────────────────────
ALERT_SCORE_THRESHOLD = 60
MIN_RR_FOR_BONUS      = 2.0
STRONG_RR_BONUS       = 2.5

# ── F. Volume confirmation ────────────────────────────────────────────────────
ENABLE_VOLUME_CONFIRMATION = True
VOLUME_LOOKBACK            = 20
VOLUME_SPIKE_MULTIPLIER    = 1.5

# ── G. Deduplication ─────────────────────────────────────────────────────────
DEDUP_LOOKBACK_HOURS = 6

# ── H. Telegram alerts ────────────────────────────────────────────────────────
TELEGRAM_ENABLED   = False
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID_HERE"

# ── Output ────────────────────────────────────────────────────────────────────
SHOW_CHART = False
LOG_FILE   = "signals.log"# Append signals here; set "" to disable