# config.py — SMC Scanner v2 — Central configuration
# ─────────────────────────────────────────────────────────────────────────────

# ── A. Exchange settings ──────────────────────────────────────────────────────
EXCHANGE   = "binance"
API_KEY    = ""
API_SECRET = ""

# ── B. Scanner settings ───────────────────────────────────────────────────────
SYMBOLS = [
    # Tier 1 — high WR in backtest, keep always
    "BTC/USDT",    # 60% WR on 1h
    "ETH/USDT",    # 55% WR on 1h
    "BNB/USDT",    # 50% WR
    "DOGE/USDT",   # 67% WR (small sample, watch)
    # Tier 2 — added back for sample size; revisit after 200+ trades
    "XRP/USDT",    # 25% WR was on 15m (broken TF) — test on 1h/4h
    "SOL/USDT",    # 20% WR was on 15m — retest on higher TF
    # Removed: AVAX/USDT (10% WR, broken on all TFs)
    "LINK/USDT",   #это сам уже добавил
]

TIMEFRAMES = ["15m", "30m", "1h", "2h", "4h", "8h", "1d"]   # 15m removed (15% WR), 4h added for structure clarity

# Higher-timeframe map used by scoring
HTF_MAP = {
    "1m":  "15m",
    "5m":  "15m",
    "15m": "1h",
    "30m": "1h",   # лучше чем 4h (слишком далеко было)
    "1h":  "4h",
    "2h":  "4h",
    "4h":  "1d",
    "8h":  "1d",
    "1d":  "1w",
}

CANDLE_LIMIT          = 300   # increased for 4h context depth
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
ALERT_SCORE_THRESHOLD = 75
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
TELEGRAM_BOT_TOKEN = "86*****3835:****************fIj*****LA"
TELEGRAM_CHAT_ID   = "**************"
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
CONFIRMATION_MIN_SCORE           = 75   # 70-74 had 29% WR so raise, but 80 gives too few trades

# Hard block flags — set False to downgrade from hard-block to score penalty
CONFIRMATION_HTF_MANDATORY       = True   # block if HTF opposes
CONFIRMATION_SWEEP_MANDATORY     = True   # block if sweep is low quality
CONFIRMATION_BOS_MANDATORY       = True   # block if BOS is weak
CONFIRMATION_OB_MANDATORY        = True   # block if no OB or FVG found
CONFIRMATION_PD_MANDATORY        = True   # MANDATORY: SHORT only in premium/eq, LONG only in discount/eq
CONFIRMATION_LIQ_TARGET_MANDATORY = False # liquidity target (softer)

# Sweep quality thresholds
SWEEP_WICK_DOMINANCE      = 0.55   # wick must be >= 55% of candle range

# BOS strength thresholds
BOS_MIN_BODY_ATR_RATIO    = 0.8    # BOS body must be >= 0.8× ATR

# Premium/Discount zone boundaries
# price at < PD_DISCOUNT_LEVEL → discount zone (good for longs)
# price at > PD_PREMIUM_LEVEL  → premium zone  (good for shorts)
PD_DISCOUNT_LEVEL         = 0.40   # lower 40% = discount zone (longs only)
PD_PREMIUM_LEVEL          = 0.60   # upper 40% = premium zone (shorts only)
# Equilibrium = 40-60% = allowed for both directions but no bonus

# Minimum distance from current price to liquidity target
CONFIRMATION_MIN_TARGET_DISTANCE = 0.005  # 0.5% minimum
# ── L. Kill Zones ─────────────────────────────────────────────────────────────
# Modes:
#   "log"    — always allow, but log KZ status to console (safe to start with)
#   "filter" — block signals outside KZ (strict, fewer signals)
#   "score"  — affect scoring only (+10 inside, -5 outside), no hard block
#   "off"    — completely disabled
KILLZONE_MODE = "log"

# ── M. Backtesting ────────────────────────────────────────────────────────────
BACKTEST_DAYS       = 90    # default lookback period in days
BACKTEST_WALK_STEP  = 3     # bars to advance per iteration (higher = faster)

# ── N. Trailing stop ──────────────────────────────────────────────────────────
# Once price moves TRAILING_STOP_TRIGGER_R in our favour,
# move stop to breakeven. Prevents -1R losses on trades that moved 1R+ our way.
# Set to 0 to disable.
TRAILING_STOP_ENABLED   = True
TRAILING_STOP_TRIGGER_R = 1.0    # move SL to BE when price reaches +1R
TRAILING_STOP_LOCK_R    = 0.0    # lock in 0R (breakeven) when triggered

# ── O. Backtest settings ──────────────────────────────────────────────────────
BACKTEST_DAYS       = 180   # 6 months for statistical validity (need 50+ trades)
BACKTEST_WALK_STEP  = 2     # bars per iteration (lower = more signals caught)
BACKTEST_INTRABAR_POLICY = "conservative"  # conservative | optimistic | close_bias | open_distance
