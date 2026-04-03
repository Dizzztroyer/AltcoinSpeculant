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
LOG_FILE   = "signals.log"

# ── Scheduler ────────────────────────────────────────────────────────────────
RUN_ON_START = True   # True = run immediately on startup, then align to next hour

# ── Multi-timeframe confluence filter ─────────────────────────────────────────
# HTF_FILTER_ENABLED : master switch — set False to disable entirely
# HTF_FILTER_STRICT  : True  = block signals that oppose HTF (hard filter)
#                      False = allow all signals, opposing HTF only loses points
HTF_FILTER_ENABLED = True
HTF_FILTER_STRICT  = True

# ── I. Order Block settings ───────────────────────────────────────────────────
# OB_LOOKBACK           : how many recent candles to scan for OBs
# OB_IMPULSE_LOOKFORWARD: how many candles ahead to look for the impulse move
# OB_MIN_IMPULSE_PCT    : minimum impulse size (fraction of price) to qualify
# OB_FVG_MIN_GAP        : minimum FVG gap size (fraction of price)
# OB_FVG_SEARCH_RANGE   : how far outside OB bounds to search for nearby FVG
#                         (as multiple of OB height)
OB_LOOKBACK            = 100
OB_IMPULSE_LOOKFORWARD = 5
OB_MIN_IMPULSE_PCT     = 0.003   # 0.3 % minimum impulse
OB_FVG_MIN_GAP         = 0.0005  # 0.05 % minimum gap
OB_FVG_SEARCH_RANGE    = 1.5     # search 1.5× OB height outside OB for FVG

# ── J. Virtual Portfolio ───────────────────────────────────────────────────────
VIRTUAL_BALANCE      = 100.0   # starting balance in USD
RISK_PER_TRADE_PCT   = 0.01    # 1% risk per trade