# backtesting.py — Historical backtest for the SMC strategy
#
# Runs the full signal pipeline on historical OHLCV data and
# evaluates outcomes candle-by-candle — no live data, no DB writes.
#
# Usage:
#   python backtesting.py                           # all symbols, 90 days
#   python backtesting.py --symbol BTC/USDT         # single symbol
#   python backtesting.py --symbol BTC/USDT --tf 1h
#   python backtesting.py --days 30                 # shorter window
#   python backtesting.py --html                    # open HTML report

from __future__ import annotations

import argparse
import sys
import time as time_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

import config
from datafeed import fetch_ohlcv
from liquidity import build_liquidity_zones, detect_sweeps
from orderblocks import find_fvgs, find_order_blocks
from signals import scan_for_signals
from structure import find_swings
from utils import log_info, log_warn, log_error


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    symbol:       str
    timeframe:    str
    direction:    str
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    signal_bar:   int          # bar index when signal was generated
    entry_bar:    int | None   # bar when entry zone was hit
    exit_bar:     int | None
    exit_price:   float | None
    outcome:      str          # 'won' | 'lost' | 'expired' | 'pending'
    rr_planned:   float
    rr_actual:    float
    pnl_r:        float        # PnL in R multiples
    score:        int
    ob_label:     str
    pd_zone:      str
    htf_bias:     str
    mfe:          float        # max favourable excursion (in R)
    mae:          float        # max adverse excursion (in R)
    signal_time:  str


@dataclass
class BacktestResult:
    symbol:     str
    timeframe:  str
    trades:     list[BacktestTrade] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.outcome == "won"]

    @property
    def losses(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.outcome == "lost"]

    @property
    def expired(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.outcome == "expired"]

    @property
    def win_rate(self) -> float:
        decisive = len(self.wins) + len(self.losses)
        return len(self.wins) / decisive * 100 if decisive else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.pnl_r for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t.pnl_r for t in self.wins)
        gross_loss = abs(sum(t.pnl_r for t in self.losses))
        return gross_win / gross_loss if gross_loss > 0 else 0.0

    @property
    def expectancy_r(self) -> float:
        decisive = len(self.wins) + len(self.losses)
        if not decisive:
            return 0.0
        return sum(t.pnl_r for t in self.wins + self.losses) / decisive

    @property
    def avg_rr_actual(self) -> float:
        vals = [t.rr_actual for t in self.wins + self.losses]
        return sum(vals) / len(vals) if vals else 0.0


# ── Core backtest engine ───────────────────────────────────────────────────────

def run_backtest(symbol: str,
                 timeframe: str,
                 days: int = 90,
                 walk_step: int = 1) -> BacktestResult:
    """
    Walk-forward backtest on historical data.

    Method:
    -------
    1. Fetch enough candles to cover `days` of history + warmup
    2. Walk through the data one candle at a time (or `walk_step`)
    3. At each bar, run scan_for_signals() on candles[0..i]
    4. For each new signal, evaluate outcome on subsequent candles
    5. Record all trades into BacktestResult

    Parameters
    ----------
    symbol     : e.g. 'BTC/USDT'
    timeframe  : e.g. '15m', '1h'
    days       : how many days of history to test
    walk_step  : advance N candles between signal checks (1 = every candle)
                 Use higher values (e.g. 5-10) for faster runs

    Notes
    -----
    - Uses the LIVE signal pipeline (signals.py + confirmation.py)
      so whatever you change in strategy logic is tested instantly
    - Signals seen in DB are NOT affected (no writes)
    - Minimum warmup = 100 candles before first signal attempt
    """
    result = BacktestResult(symbol=symbol, timeframe=timeframe)

    # ── Fetch data ────────────────────────────────────────────────────────────
    bars_per_day = _bars_per_day(timeframe)
    needed       = days * bars_per_day + 150    # +150 warmup
    needed       = min(needed, 1000)            # exchange cap

    log_info(f"[BT] {symbol} {timeframe} — fetching {needed} candles ({days}d) ...")
    df = fetch_ohlcv(symbol, timeframe, limit=needed)

    if df.empty or len(df) < 150:
        log_warn(f"[BT] not enough data for {symbol} {timeframe}")
        return result

    log_info(f"[BT] {symbol} {timeframe} — {len(df)} candles loaded, walking ...")

    warmup       = 100
    seen_hashes: set[str] = set()

    # ── Walk-forward loop ─────────────────────────────────────────────────────
    i = warmup
    while i < len(df) - 10:
        window = df.iloc[:i].copy()

        try:
            signals = _scan_no_db(symbol, timeframe, window)
        except Exception as exc:
            log_warn(f"[BT] scan error at bar {i}: {exc}")
            i += walk_step
            continue

        for sig in signals:
            sig_hash = _sig_hash(sig)
            if sig_hash in seen_hashes:
                i += walk_step
                continue
            seen_hashes.add(sig_hash)

            # Evaluate on bars after signal
            future = df.iloc[i:i + config.EVALUATION_LOOKAHEAD_BARS + 10]
            trade  = _evaluate_trade(sig, future, i)
            if trade:
                result.trades.append(trade)
                outcome_str = f"{trade.outcome.upper():<8} {trade.pnl_r:+.2f}R"
                log_info(f"[BT]   {symbol} {timeframe} bar={i:4d} "
                         f"{trade.direction.upper():<6} → {outcome_str}")

        i += walk_step

    log_info(f"[BT] {symbol} {timeframe} done — "
             f"{result.total} trades  "
             f"WR={result.win_rate:.1f}%  "
             f"Total={result.total_r:+.2f}R  "
             f"PF={result.profit_factor:.2f}")
    return result


def _scan_no_db(symbol: str, timeframe: str, df: pd.DataFrame) -> list[dict]:
    """
    Run scan_for_signals but bypass DB deduplication.
    We patch journal.get_recent_signals to always return [] during backtest.
    """
    import journal as _journal
    original = _journal.get_recent_signals

    def _no_dedup(*args, **kwargs):
        return []

    _journal.get_recent_signals = _no_dedup
    try:
        return scan_for_signals(symbol, timeframe, df)
    finally:
        _journal.get_recent_signals = original


def _evaluate_trade(sig: dict, future: pd.DataFrame,
                    signal_bar: int) -> BacktestTrade | None:
    """Walk future candles to determine trade outcome."""
    direction   = sig["direction"]
    entry_low   = sig["entry_low"]
    entry_high  = sig["entry_high"]
    stop_loss   = sig["stop"]
    take_profit = sig["tp"]
    mid_entry   = (entry_low + entry_high) / 2
    price_risk  = abs(mid_entry - stop_loss)

    if price_risk <= 0:
        return None

    rr_planned = round(abs(take_profit - mid_entry) / price_risk, 2)

    entry_bar   = None
    exit_bar    = None
    exit_price  = None
    outcome     = "expired"
    mfe         = 0.0
    mae         = 0.0
    best        = mid_entry
    worst       = mid_entry

    for offset, (_, candle) in enumerate(future.iterrows()):
        h, l, c = candle["high"], candle["low"], candle["close"]
        bar_abs = signal_bar + offset

        # Entry trigger
        if entry_bar is None:
            triggered = ((direction == "long"  and l <= entry_high) or
                         (direction == "short" and h >= entry_low))
            if not triggered:
                continue
            entry_bar = bar_abs

        # MFE / MAE in price terms
        if direction == "long":
            best  = max(best, h)
            worst = min(worst, l)
        else:
            best  = min(best, l)
            worst = max(worst, h)

        mfe = abs(best  - mid_entry) / price_risk
        mae = abs(worst - mid_entry) / price_risk
        if direction == "long":
            mae = -mae if worst < mid_entry else mae
        else:
            mae = -mae if worst > mid_entry else mae

        # Trailing stop
        if getattr(config, "TRAILING_STOP_ENABLED", False):
            trigger_r = getattr(config, "TRAILING_STOP_TRIGGER_R", 1.0)
            lock_r    = getattr(config, "TRAILING_STOP_LOCK_R", 0.0)
            price_risk = abs(mid_entry - stop_loss)
            if price_risk > 0:
                if direction == "long":
                    moved_r = (h - mid_entry) / price_risk
                    if moved_r >= trigger_r:
                        new_sl = mid_entry + lock_r * price_risk
                        if new_sl > stop_loss:
                            stop_loss = new_sl
                else:
                    moved_r = (mid_entry - l) / price_risk
                    if moved_r >= trigger_r:
                        new_sl = mid_entry - lock_r * price_risk
                        if new_sl < stop_loss:
                            stop_loss = new_sl

        # TP / SL check
        tp_hit = (direction == "long"  and h >= take_profit) or \
                 (direction == "short" and l <= take_profit)
        sl_hit = (direction == "long"  and l <= stop_loss) or \
                 (direction == "short" and h >= stop_loss)

        if tp_hit and sl_hit:
            outcome    = "won" if (direction == "long" and c > mid_entry) or \
                                   (direction == "short" and c < mid_entry) else "lost"
            exit_price = take_profit if outcome == "won" else stop_loss
            exit_bar   = bar_abs
            break
        elif tp_hit:
            outcome, exit_price, exit_bar = "won", take_profit, bar_abs
            break
        elif sl_hit:
            outcome, exit_price, exit_bar = "lost", stop_loss, bar_abs
            break

    if exit_price is None:
        exit_price = future["close"].iloc[-1] if not future.empty else mid_entry

    rr_actual = abs(exit_price - mid_entry) / price_risk
    if outcome == "lost":
        rr_actual = -rr_actual

    pnl_r = config.DEFAULT_RR_RATIO if outcome == "won" else \
            -1.0 if outcome == "lost" else \
            (exit_price - mid_entry) / price_risk if direction == "long" else \
            (mid_entry - exit_price) / price_risk

    return BacktestTrade(
        symbol=sig["symbol"],
        timeframe=sig["timeframe"],
        direction=direction,
        entry_price=mid_entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        signal_bar=signal_bar,
        entry_bar=entry_bar,
        exit_bar=exit_bar,
        exit_price=round(exit_price, 4),
        outcome=outcome,
        rr_planned=rr_planned,
        rr_actual=round(rr_actual, 3),
        pnl_r=round(pnl_r, 3),
        score=sig.get("conf_score", sig.get("score", 0)),
        ob_label=sig.get("ob_label", ""),
        pd_zone=sig.get("pd_zone", ""),
        htf_bias=sig.get("htf_bias", ""),
        mfe=round(mfe, 3),
        mae=round(mae, 3),
        signal_time=sig.get("_bar_time", ""),
    )


# ── Multi-symbol runner ────────────────────────────────────────────────────────

def run_full_backtest(symbols: list[str] | None = None,
                      timeframes: list[str] | None = None,
                      days: int = 90) -> list[BacktestResult]:
    symbols    = symbols    or config.SYMBOLS
    timeframes = timeframes or config.TIMEFRAMES
    results    = []

    for sym in symbols:
        for tf in timeframes:
            try:
                r = run_backtest(sym, tf, days=days)
                results.append(r)
                time_mod.sleep(0.5)
            except Exception as exc:
                log_error(f"[BT] {sym} {tf}: {exc}")

    return results


# ── Report ────────────────────────────────────────────────────────────────────

def print_backtest_report(results: list[BacktestResult]) -> None:
    SEP  = "═" * 70
    sep2 = "─" * 70

    print(f"\n{SEP}")
    print(f"  📈  SMC BACKTEST REPORT  —  "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(SEP)

    all_trades: list[BacktestTrade] = []

    for r in results:
        all_trades.extend(r.trades)
        if r.total == 0:
            print(f"  {r.symbol:<14} {r.timeframe:<6}  no signals")
            continue

        decisive = len(r.wins) + len(r.losses)
        sign = "+" if r.total_r >= 0 else ""
        print(f"\n  {r.symbol:<14} {r.timeframe}")
        print(f"  {'Trades':<18}: {r.total}  (W:{len(r.wins)} L:{len(r.losses)} "
              f"Exp:{len(r.expired)})")
        print(f"  {'Win rate':<18}: {r.win_rate:.1f}%")
        print(f"  {'Total R':<18}: {sign}{r.total_r:.2f}R")
        print(f"  {'Profit factor':<18}: {r.profit_factor:.2f}")
        print(f"  {'Expectancy':<18}: {r.expectancy_r:+.2f}R/trade")
        print(f"  {'Avg RR actual':<18}: {r.avg_rr_actual:.2f}")

        # Per-score breakdown
        if r.trades:
            by_score = {}
            for t in r.trades:
                bucket = (t.score // 10) * 10
                if bucket not in by_score:
                    by_score[bucket] = {"w": 0, "l": 0, "r": 0.0}
                by_score[bucket]["r"] += t.pnl_r
                if t.outcome == "won":
                    by_score[bucket]["w"] += 1
                elif t.outcome == "lost":
                    by_score[bucket]["l"] += 1

            print(f"  {'Score bucket':<18}  W   L   R")
            for bucket in sorted(by_score):
                d = by_score[bucket]
                total_bucket = d["w"] + d["l"]
                wr = d["w"] / total_bucket * 100 if total_bucket else 0
                sign = "+" if d["r"] >= 0 else ""
                print(f"    {bucket:3d}–{bucket+9:<3d}           "
                      f"{d['w']:3d} {d['l']:3d}  {sign}{d['r']:.2f}R  "
                      f"WR={wr:.0f}%")
        print(sep2)

    # ── Aggregate ──────────────────────────────────────────────────────────────
    if all_trades:
        all_wins   = [t for t in all_trades if t.outcome == "won"]
        all_losses = [t for t in all_trades if t.outcome == "lost"]
        all_r      = sum(t.pnl_r for t in all_trades)
        decisive   = len(all_wins) + len(all_losses)
        wr         = len(all_wins) / decisive * 100 if decisive else 0
        gw         = sum(t.pnl_r for t in all_wins)
        gl         = abs(sum(t.pnl_r for t in all_losses))
        pf         = gw / gl if gl > 0 else 0
        exp        = all_r / decisive if decisive else 0

        print(f"\n  AGGREGATE  ({len(all_trades)} trades across all symbols)")
        sign = "+" if all_r >= 0 else ""
        print(f"  Win rate   : {wr:.1f}%")
        print(f"  Total R    : {sign}{all_r:.2f}R")
        print(f"  Prof factor: {pf:.2f}")
        print(f"  Expectancy : {exp:+.2f}R/trade")

        # Simulate equity curve at 1% risk
        balance = 100.0
        for t in all_trades:
            balance += balance * 0.01 * t.pnl_r
        print(f"  Simulated  : $100 → ${balance:.2f} (1% risk/trade)")

    print(SEP)


def generate_backtest_html(results: list[BacktestResult],
                            output: str = "backtest.html") -> str:
    """Generate an HTML backtest report with equity curve."""
    all_trades = [t for r in results for t in r.trades]
    if not all_trades:
        with open(output, "w") as f:
            f.write("<h1>No backtest trades found</h1>")
        return output

    # Equity curve (1% risk compounding)
    balance  = 100.0
    eq_dates: list[str] = []
    eq_vals:  list[float] = []
    for t in all_trades:
        balance += balance * 0.01 * t.pnl_r
        eq_dates.append(t.signal_time[:10] or str(len(eq_dates)))
        eq_vals.append(float(round(balance, 2)))

    # Trade rows
    rows_html = ""
    for t in all_trades:
        color  = "#00e676" if t.outcome == "won" else \
                 "#ff4c4c" if t.outcome == "lost" else "#888"
        emoji  = "✅" if t.outcome == "won" else \
                 "❌" if t.outcome == "lost" else "⏱"
        sign   = "+" if t.pnl_r >= 0 else ""
        rows_html += f"""<tr>
            <td>{t.symbol}</td><td>{t.timeframe}</td>
            <td class="{'long' if t.direction=='long' else 'short'}">{t.direction.upper()}</td>
            <td>{t.score}</td>
            <td style="color:{color}">{emoji} {t.outcome}</td>
            <td style="color:{color};font-weight:bold">{sign}{t.pnl_r:.2f}R</td>
            <td>{t.rr_planned:.2f}</td>
            <td>{t.mfe:.2f}R</td>
            <td>{t.mae:.2f}R</td>
            <td>{t.htf_bias}</td>
            <td>{t.pd_zone}</td>
            <td style="font-size:11px">{t.ob_label[:30]}</td>
        </tr>\n"""

    all_wins   = [t for t in all_trades if t.outcome == "won"]
    all_losses = [t for t in all_trades if t.outcome == "lost"]
    decisive   = len(all_wins) + len(all_losses)
    wr         = round(len(all_wins) / decisive * 100, 1) if decisive else 0
    total_r    = round(sum(t.pnl_r for t in all_trades), 2)
    gw = sum(t.pnl_r for t in all_wins)
    gl = abs(sum(t.pnl_r for t in all_losses))
    pf = round(gw / gl, 2) if gl > 0 else 0
    exp_r = round(total_r / decisive, 2) if decisive else 0
    sign  = "+" if total_r >= 0 else ""

    import json
    json_dates = json.dumps(eq_dates)
    json_vals  = json.dumps([float(v) for v in eq_vals])
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>SMC Backtest Report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;font-family:'Courier New',monospace;font-size:13px;padding:20px}}
h1{{font-size:20px;color:#58a6ff;margin-bottom:4px}}
.sub{{color:#8b949e;font-size:11px;margin-bottom:20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}}
.card .label{{color:#8b949e;font-size:10px;text-transform:uppercase;margin-bottom:4px}}
.card .value{{font-size:20px;font-weight:bold}}
.green{{color:#00e676}}.red{{color:#ff4c4c}}.blue{{color:#58a6ff}}.yellow{{color:#ffd700}}
.chart-box{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:20px}}
.chart-box h3{{color:#8b949e;font-size:11px;margin-bottom:12px;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px}}
th{{background:#21262d;color:#8b949e;font-size:10px;padding:8px 10px;text-align:left;text-transform:uppercase}}
td{{padding:7px 10px;border-top:1px solid #21262d;font-size:12px}}
tr:hover td{{background:#1c2128}}
.long{{color:#00e676}}.short{{color:#ff4c4c}}
h2{{color:#58a6ff;margin:16px 0 10px;font-size:14px}}
</style></head><body>
<h1>📈 SMC Backtest Report</h1>
<div class="sub">Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | {len(all_trades)} trades</div>
<div class="grid">
  <div class="card"><div class="label">Total Trades</div><div class="value blue">{len(all_trades)}</div></div>
  <div class="card"><div class="label">Win Rate</div><div class="value blue">{wr}%</div></div>
  <div class="card"><div class="label">Total R</div><div class="value {'green' if total_r>=0 else 'red'}">{sign}{total_r}R</div></div>
  <div class="card"><div class="label">Profit Factor</div><div class="value {'green' if pf>=1 else 'red'}">{pf}</div></div>
  <div class="card"><div class="label">Expectancy</div><div class="value {'green' if exp_r>=0 else 'red'}">{exp_r:+.2f}R</div></div>
  <div class="card"><div class="label">Sim Balance ($100)</div><div class="value {'green' if eq_vals[-1]>=100 else 'red'}">${eq_vals[-1]:.2f}</div></div>
  <div class="card"><div class="label">Won / Lost</div><div class="value">✅{len(all_wins)} ❌{len(all_losses)}</div></div>
  <div class="card"><div class="label">Expired</div><div class="value yellow">{sum(1 for t in all_trades if t.outcome=='expired')}</div></div>
</div>
<div class="chart-box"><h3>Equity Curve (1% risk)</h3>
<canvas id="eq" height="120"></canvas></div>
<h2>Trade Log</h2>
<table><thead><tr>
<th>Symbol</th><th>TF</th><th>Dir</th><th>Score</th><th>Outcome</th>
<th>P&amp;L R</th><th>RR plan</th><th>MFE</th><th>MAE</th>
<th>HTF</th><th>Zone</th><th>OB</th>
</tr></thead><tbody>{rows_html}</tbody></table>
<script>
new Chart(document.getElementById('eq'),{{
  type:'line',
  data:{{labels:{json_dates},datasets:[{{
    data:{json_vals},borderColor:'#58a6ff',
    backgroundColor:'rgba(88,166,255,0.08)',
    fill:true,tension:0.3,pointRadius:2,
  }}]}},
  options:{{plugins:{{legend:{{display:false}}}},
    scales:{{
      x:{{ticks:{{color:'#8b949e',maxTicksLimit:10}},grid:{{color:'#21262d'}}}},
      y:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}}
    }}}}
}});
</script></body></html>"""

    with open(output, "w", encoding="utf-8") as f:
        f.write(html)
    log_info(f"[BT] HTML report saved → {output}")
    return output


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bars_per_day(timeframe: str) -> int:
    mapping = {"1m": 1440, "5m": 288, "15m": 96, "30m": 48,
               "1h": 24, "4h": 6, "1d": 1}
    return mapping.get(timeframe, 48)


def _sig_hash(sig: dict) -> str:
    import hashlib
    key = f"{sig['direction']}|{round(sig['entry_low'],2)}|{round(sig['entry_high'],2)}"
    return hashlib.md5(key.encode()).hexdigest()[:10]


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="SMC Backtester")
    p.add_argument("--symbol",  default=None)
    p.add_argument("--tf",      default=None)
    p.add_argument("--days",    type=int, default=90)
    p.add_argument("--step",    type=int, default=3,
                   help="Walk step (bars between checks, higher=faster)")
    p.add_argument("--html",    action="store_true")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    symbols    = [args.symbol] if args.symbol else config.SYMBOLS
    timeframes = [args.tf]     if args.tf     else config.TIMEFRAMES

    log_info(f"[BT] Starting backtest: {symbols} × {timeframes} × {args.days}d")

    results = []
    for sym in symbols:
        for tf in timeframes:
            r = run_backtest(sym, tf, days=args.days, walk_step=args.step)
            results.append(r)

    print_backtest_report(results)

    if args.html:
        import webbrowser
        from pathlib import Path
        path = generate_backtest_html(results)
        if not args.no_open:
            webbrowser.open(f"file://{Path(path).resolve()}")


if __name__ == "__main__":
    main()