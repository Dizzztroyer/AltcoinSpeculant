# SMC Crypto Scanner — Admin / Tradeswein Logic

A fully self-contained Python scanner that applies **Smart Money Concepts (SMC)**
to live crypto market data and generates trade setups.

---

## Strategy overview

| Step | What it does |
|------|-------------|
| 1. Market context | Classifies trend as *bullish / bearish / range* via EMA + swing structure |
| 2. Liquidity mapping | Marks swing highs, swing lows, equal highs, equal lows |
| 3. Sweep detection | Detects wick-through-and-reject events that take resting orders |
| 4. Structure break | Confirms BOS or MBOS after the sweep |
| 5. Signal output | Generates entry zone, stop-loss, take-profit, and a plain-English reason |

**Long setup**  
Price sweeps below lows → closes back above → bullish BOS fires → go long.

**Short setup**  
Price sweeps above highs → closes back below → bearish BOS fires → go short.

---

## Project layout

```
smc_scanner/
├── main.py          — Entry point; CLI flags; scan loop
├── config.py        — All tunable parameters
├── datafeed.py      — CCXT OHLCV fetch layer
├── structure.py     — Swing detection, trend, BOS/MBOS
├── liquidity.py     — Liquidity zones, equal H/L, sweep detection
├── signals.py       — Signal assembly (long / short)
├── charting.py      — Plotly candlestick + annotation chart
├── utils.py         — Logging, EMA, ATR, signal formatting
├── requirements.txt
└── README.md
```

---

## Installation

### 1. Clone / copy the folder

```bash
cd smc_scanner
```

### 2. Create a virtual environment (recommended)

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Running the scanner

### One-shot scan (all symbols × timeframes in config.py)

```bash
python main.py
```

### Continuous loop

```bash
python main.py --loop
```

### Single pair / timeframe

```bash
python main.py --symbol BTC/USDT --tf 15m
```

### Suppress Plotly chart popup

```bash
python main.py --no-chart
```

### All options combined

```bash
python main.py --symbol ETH/USDT --tf 1h --loop --no-chart
```

---

## Sample output

```
  ╔═══════════════════════════════════════════════╗
  ║     SMC CRYPTO SCANNER  — Admin / Tradeswein  ║
  ║   Liquidity · Structure · Sweep · BOS/MBOS    ║
  ╚═══════════════════════════════════════════════╝

  Symbol         TF       Context      Candles
  --------------------------------------------------
  BTC/USDT       15m      range        200
  BTC/USDT       1h       bullish      200
  ETH/USDT       15m      bearish      200
  ETH/USDT       1h       range        200

========================================================
  SMC SIGNAL — 2024-11-14 09:42:11 UTC
========================================================
  Symbol     : BTC/USDT
  Timeframe  : 15m
  Context    : Range
  Liq sweep  : Above highs (High @ 67280.0000)
  Structure  : BEARISH BOS @ 66900.0000
  Signal     : SHORT 🔴
  Entry zone : 66900 – 67280
  Stop Loss  : 67415
  Take Profit: 66200
  Reason     : Swept High @ 67280.0000, closed back below,
               then BEARISH BOS @ 66900.0000 confirmed
               → bearish continuation expected
========================================================
```

---

## Configuration reference (`config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EXCHANGE` | `"binance"` | CCXT exchange id |
| `SYMBOLS` | BTC/USDT, ETH/USDT … | Symbols to scan |
| `TIMEFRAMES` | `["15m","1h"]` | Timeframes to scan |
| `CANDLE_LIMIT` | `200` | Candles fetched per request |
| `SWING_LOOKBACK` | `5` | Bars on each side to confirm a swing |
| `BOS_THRESHOLD` | `0.001` | Min break size (0.10 %) to confirm BOS |
| `EQH_EQL_TOLERANCE` | `0.0015` | Max delta to call two levels "equal" |
| `SWEEP_WICK_FACTOR` | `0.5` | Min wick size relative to candle range |
| `SWEEP_LOOKBACK` | `30` | Recent candles checked for sweeps |
| `DEFAULT_SL_BUFFER` | `0.002` | Extra buffer beyond wick for stop |
| `DEFAULT_RR_RATIO` | `2.0` | Minimum RR ratio for take-profit |
| `TREND_EMA_FAST` | `21` | Fast EMA period for trend |
| `TREND_EMA_SLOW` | `55` | Slow EMA period for trend |
| `SCAN_INTERVAL_SECONDS` | `60` | Loop sleep time |
| `SHOW_CHART` | `True` | Open Plotly chart on signal |
| `LOG_FILE` | `"signals.log"` | File to append signals; `""` to disable |

---

## No API key required

The scanner uses **public market data** (no API key needed for Binance OHLCV).
Leave `API_KEY` and `API_SECRET` empty in `config.py`.

---

## Disclaimer

This software is for **educational and research purposes only**.  
It does **not** place orders or manage positions.  
Always backtest and apply proper risk management before using any signal in live trading.