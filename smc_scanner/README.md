# SMC Crypto Scanner — v2 Signal Engine

A scanner + journal + evaluator + alerting system built on Smart Money Concepts.
**Not an auto-trading bot.** Generates, stores, scores, and evaluates setups.

---

## What v2 adds over v1

| Feature | v1 | v2 |
|---|---|---|
| Signal detection | ✅ | ✅ |
| SQLite journal | ❌ | ✅ |
| Signal lifecycle (won/lost/expired) | ❌ | ✅ |
| Signal scoring 0-100 | ❌ | ✅ |
| HTF alignment filter | ❌ | ✅ |
| Volume confirmation | ❌ | ✅ |
| Telegram alerts | ❌ | ✅ |
| Deduplication / spam control | ❌ | ✅ |
| MFE / MAE tracking | ❌ | ✅ |

---

## Project layout

```
smc_scanner/
├── main.py        — entry point + scan loop
├── config.py      — all settings (exchange, DB, scoring, Telegram)
├── datafeed.py    — CCXT OHLCV layer
├── structure.py   — swing detection, trend, BOS/MBOS
├── liquidity.py   — liquidity zones, sweeps
├── signals.py     — signal assembly (entry/SL/TP)
├── scoring.py     — 0-100 quality scoring
├── journal.py     — SQLite persistence
├── evaluator.py   — signal lifecycle tracking
├── alerts.py      — Telegram delivery
├── charting.py    — Plotly chart (optional)
├── utils.py       — logging, EMA, ATR, formatting
├── requirements.txt
└── README.md
```

---

## Installation

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

---

## How to run

```bash
# One-shot scan
python main.py

# Hourly loop
python main.py --loop

# Single pair / timeframe
python main.py --symbol BTC/USDT --tf 15m

# No charts
python main.py --no-chart

# Show DB summary only
python main.py --summary

# All options
python main.py --symbol ETH/USDT --tf 1h --loop --no-chart
```

---

## Console output legend

```
[DB]    — database operation
[SCAN]  — market scanning
[EVAL]  — open signal evaluation
[SCORE] — scoring breakdown
[ALERT] — Telegram alert sent
[SKIP]  — duplicate alert suppressed
[LOOP]  — scheduler info
```

---

## Signal journal

Every signal is stored in `signals.db` (SQLite) with a full lifecycle:

| Field | Description |
|---|---|
| `status` | pending → triggered → won / lost / expired |
| `score` | 0-100 quality rating |
| `entry_hit` | whether price entered the zone |
| `mfe` | max favorable excursion from entry |
| `mae` | max adverse excursion from entry |
| `expires_at` | auto-expiry timestamp |
| `signal_hash` | dedup key |

### Signal statuses

| Status | Meaning |
|---|---|
| `pending` | waiting for price to reach entry zone |
| `triggered` | price entered the zone |
| `won` | take-profit was hit first |
| `lost` | stop-loss was hit first |
| `expired` | neither SL nor TP hit before expiry |
| `cancelled` | manually cancelled (future use) |

---

## Signal evaluation

The evaluator runs at the start of every cycle and checks all open signals:

1. Fetch fresh candles after the signal was created
2. Walk candle-by-candle:
   - If price enters the entry zone → mark `triggered`
   - Track best/worst price for MFE / MAE
   - If TP is hit first → `won`; if SL is hit first → `lost`
   - If expires_at is reached without conclusion → `expired`

### Ambiguity rule

When both SL and TP fall within the same candle (rare):
- **Long**: if close > mid_entry → TP assumed first (`won`), else `lost`
- **Short**: if close < mid_entry → TP assumed first (`won`), else `lost`

This conservative rule avoids inflating the win rate.

---

## Scoring

Scores are computed by `scoring.py` immediately after a signal is detected:

| Component | Points |
|---|---|
| Valid setup baseline | +40 |
| RR >= 2.0 | +15 |
| RR >= 2.5 | +20 (replaces +15) |
| HTF bias aligned | +15 |
| HTF bias opposing | -10 |
| Volume spike confirmed | +10 |
| Clear sweep candle (wick ≥ 70 %) | +10 |
| Dead market (ATR very low) | -10 |
| TP too close (< 1 % from entry) | -10 |
| Duplicate in last N hours | -20 |

Final score is clamped to **[0, 100]**.

---

## Higher timeframe alignment

The HTF is determined by `config.HTF_MAP`:

```python
"15m" → "1h"
"1h"  → "4h"
"4h"  → "1d"
```

- If HTF context matches signal direction → +15 points
- If HTF opposes → -10 points
- If HTF is range or unavailable → neutral (0)

---

## Volume confirmation

Controlled by `config.py`:

```python
ENABLE_VOLUME_CONFIRMATION = True
VOLUME_LOOKBACK            = 20     # rolling average window
VOLUME_SPIKE_MULTIPLIER    = 1.5    # must be 1.5× avg to qualify
```

Checks the last 2 candles (sweep + BOS area) against the rolling average.

---

## Telegram alerts

1. Set in `config.py`:
```python
TELEGRAM_ENABLED   = True
TELEGRAM_BOT_TOKEN = "your_token"
TELEGRAM_CHAT_ID   = "your_chat_id"
ALERT_SCORE_THRESHOLD = 60
```

2. Alerts are suppressed if:
   - Score is below the threshold
   - The same symbol/tf/direction was alerted within `DEDUP_LOOKBACK_HOURS`
   - `alert_sent` flag is already set in the DB

### Getting a Telegram bot token

1. Open Telegram → search for `@BotFather`
2. Send `/newbot` and follow prompts
3. Copy the token into `TELEGRAM_BOT_TOKEN`
4. Find your chat ID: send any message to the bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Copy `message.chat.id` into `TELEGRAM_CHAT_ID`

---

## Limitations and assumptions

- **No intra-candle order is known.** When both SL and TP are within one candle
  the scanner uses the close price to decide outcome (see Ambiguity rule above).
- **Exchange data is public.** No API key is required for Binance OHLCV.
- **HTF fetch adds one extra API call per signal.** Rate-limited automatically.
- **This is not financial advice.** Always apply your own risk management.

---

## Disclaimer

Educational / research use only. Does not place orders. 
Past signal performance does not guarantee future results.y proper risk management before using any signal in live trading.