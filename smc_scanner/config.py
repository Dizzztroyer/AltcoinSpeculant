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
    # Added symbols
    "XRP/USDT",
    "AVAX/USDT",
    "DOGE/USDT",
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

CANDLE_LIMIT          = 1000
SCAN_INTERVAL_MINUTES = 30    # run every 30 minutes, aligned to :00 and :30

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
TELEGRAM_ENABLED   = True
TELEGRAM_BOT_TOKEN = "8*********17qzEr3KLhNmfIjLIVq9LA"
TELEGRAM_CHAT_ID   = "-10**********089"

# ── Output ────────────────────────────────────────────────────────────────────
SHOW_CHART = False
LOG_FILE   = "signals.log"

# ── Scheduler ────────────────────────────────────────────────────────────────
RUN_ON_START = True   # True = run immediately on startup, then align to next interval

# Local timezone for midnight daily report (IANA format)
# Examples: 'Europe/Kiev', 'Europe/London', 'America/New_York', 'Asia/Singapore'
LOCAL_TIMEZONE = "Europe/Kiev"

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

# ── K. Multi-layer confirmation engine ────────────────────────────────────────
# Minimum total score to allow a trade (0-100)
CONFIRMATION_MIN_SCORE           = 70

# Hard block flags — set False to downgrade from hard-block to score penalty
CONFIRMATION_HTF_MANDATORY       = True   # block if HTF opposes
CONFIRMATION_SWEEP_MANDATORY     = True   # block if sweep is low quality
CONFIRMATION_BOS_MANDATORY       = True   # block if BOS is weak
CONFIRMATION_OB_MANDATORY        = True   # block if no OB or FVG found
CONFIRMATION_PD_MANDATORY        = False  # premium/discount (softer)
CONFIRMATION_LIQ_TARGET_MANDATORY = False # liquidity target (softer)

# Sweep quality thresholds
SWEEP_WICK_DOMINANCE      = 0.55   # wick must be >= 55% of candle range

# BOS strength thresholds
BOS_MIN_BODY_ATR_RATIO    = 0.8    # BOS body must be >= 0.8× ATR

# Premium/Discount zone boundaries
# price at < PD_DISCOUNT_LEVEL → discount zone (good for longs)
# price at > PD_PREMIUM_LEVEL  → premium zone  (good for shorts)
PD_DISCOUNT_LEVEL         = 0.35   # lower 35% of range
PD_PREMIUM_LEVEL          = 0.65   # upper 35% of range

# Minimum distance from current price to liquidity target
CONFIRMATION_MIN_TARGET_DISTANCE = 0.005  # 0.5% minimum
# ── Q. Multi-layer HTF context ────────────────────────────────────────────────
# DEEP_HTF_ENABLED: check 2-3 HTF layers instead of one
#   True  = 1h signal checks 4h + 1d + 1w (proportional scoring)
#   False = original behaviour (single HTF from HTF_MAP)
DEEP_HTF_ENABLED = True

# Extended HTF map for deep multi-layer analysis
# Structure: signal_tf → [(htf_tf, weight_pts), ...]
# Total weights per row should sum to ~25 (max HTF score)
# Nearest HTF gets highest weight (most immediate structure)
DEEP_HTF_MAP = {
    "5m":  [("15m", 10), ("1h",  10), ("4h",  5)],
    "15m": [("1h",  12), ("4h",  8),  ("1d",  5)],
    "30m": [("4h",  12), ("1d",  8),  ("1w",  5)],
    "1h":  [("4h",  12), ("1d",  8),  ("1w",  5)],
    "2h":  [("4h",  10), ("1d",  8),  ("1w",  7)],
    "4h":  [("1d",  12), ("1w",  8),  ("1M",  5)],
    "8h":  [("1d",  10), ("1w",  8),  ("1M",  7)],
    "1d":  [("1w",  12), ("1M",  13)],
    "1w":  [("1M",  25)],
}

# ── R. Cascade ambiguity resolution ──────────────────────────────────────────
# When SL and TP both fall in the same candle, try to resolve via lower TF.
#   True  = fetch smaller TF data to determine which was hit first
#   False = use conservative close-price rule (original behaviour)
CASCADE_AMBIGUITY_ENABLED = True

# How many TF levels to try before falling back to conservative rule
CASCADE_MAX_DEPTH = 3