#!/usr/bin/env python3
"""
main.py — SMC Scanner v2

Usage:
    python main.py                   # run once immediately
    python main.py --loop            # hourly scheduler (aligned to clock)
    python main.py --symbol BTC/USDT --tf 15m
    python main.py --no-chart
    python main.py --summary         # show DB status counts and exit
"""

import argparse
import sys

import config
import journal
import evaluator
import scoring
import alerts
from charting import draw_chart
from datafeed import fetch_all
from liquidity import build_liquidity_zones, detect_sweeps
from scheduler import run_scheduler
from signals import scan_for_signals
from structure import find_swings, get_market_context
from utils import format_signal, log_error, log_info, log_signal, log_warn


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SMC Scanner v2")
    p.add_argument("--symbol",   default=None, help="Single symbol, e.g. BTC/USDT")
    p.add_argument("--tf",       default=None, help="Single timeframe, e.g. 15m")
    p.add_argument("--loop",     action="store_true", help="Hourly scheduler mode")
    p.add_argument("--no-chart", action="store_true", help="Suppress Plotly charts")
    p.add_argument("--limit",    type=int, default=None, help="Candle count override")
    p.add_argument("--summary",  action="store_true", help="Print DB summary and exit")
    return p.parse_args()


# ── Context table (printed before each scan) ──────────────────────────────────

def _print_context_table(symbols: list[str], timeframes: list[str]) -> None:
    data = fetch_all(symbols=symbols, timeframes=timeframes)
    print()
    print(f"  {'Symbol':<14} {'TF':<8} {'Context':<12} Candles")
    print("  " + "-" * 46)
    for (sym, tf), df in data.items():
        if df.empty:
            continue
        ctx = get_market_context(find_swings(df))
        print(f"  {sym:<14} {tf:<8} {ctx:<12} {len(df)}")
    print()


# ── Core scan-evaluate-score-alert cycle ──────────────────────────────────────

# These are set by main() before the scheduler starts,
# so run_cycle() can be a plain zero-argument callable.
_symbols:    list[str] = []
_timeframes: list[str] = []
_show_chart: bool      = False
_limit:      int | None = None


def run_cycle() -> None:
    """
    One complete pass:
      1. evaluate previously open signals
      2. fetch fresh data + scan for new setups
      3. score each setup
      4. save to DB (dedup by hash)
      5. send Telegram alerts for high-scoring signals
    """

    # ── Step 1: evaluate open signals ────────────────────────────────────────
    evaluator.evaluate_open_signals()

    # ── Step 2: fetch + scan ──────────────────────────────────────────────────
    data = fetch_all(symbols=_symbols, timeframes=_timeframes)

    for (sym, tf), df in data.items():
        if df.empty:
            log_warn(f"[SCAN] skipping {sym} {tf}: empty data")
            continue

        if _limit:
            df = df.tail(_limit)

        log_info(f"[SCAN] {sym} [{tf}]  ({len(df)} candles)")

        try:
            raw_signals = scan_for_signals(sym, tf, df)
        except Exception as exc:
            log_error(f"[SCAN] error on {sym} {tf}: {exc}")
            continue

        if not raw_signals:
            log_info("[SCAN]   no setup found")
            continue

        for sig in raw_signals:

            # ── Step 3: score ─────────────────────────────────────────────────
            try:
                score, htf_bias = scoring.score_signal(sig, df)
            except Exception as exc:
                log_warn(f"[SCORE] error: {exc}")
                score, htf_bias = 40, ""

            vol_ok = (scoring._volume_confirmed(df)
                      if config.ENABLE_VOLUME_CONFIRMATION else False)

            # ── Step 4: save ──────────────────────────────────────────────────
            signal_id = journal.save_signal(sig, score=score,
                                            higher_tf_bias=htf_bias)
            if signal_id is None:
                log_info(f"[DB] duplicate signal ignored — {sym} {tf} "
                         f"{sig['direction'].upper()}")
                continue

            log_info(f"[DB] saved signal #{signal_id}  {sym} {tf} "
                     f"{sig['direction'].upper()}  score={score}")

            log_signal(format_signal(sig))

            # ── Step 5: alert ─────────────────────────────────────────────────
            alerts.maybe_send_alert(signal_id, sig, score, htf_bias, vol_ok)

            # ── Optional chart ────────────────────────────────────────────────
            if _show_chart and config.SHOW_CHART:
                try:
                    df_s   = find_swings(df)
                    zones  = build_liquidity_zones(df_s)
                    sweeps = detect_sweeps(df_s, zones)
                    draw_chart(df_s, sym, tf, zones, sweeps, sig)
                except Exception as exc:
                    log_warn(f"[CHART] error: {exc}")

    journal.print_summary()


# ── Entry point ───────────────────────────────────────────────────────────────

BANNER = r"""
  ╔══════════════════════════════════════════════════╗
  ║   SMC CRYPTO SCANNER  v2  — signal engine        ║
  ║   Sweep · BOS · Score · Journal · Alerts         ║
  ╚══════════════════════════════════════════════════╝
"""


def main() -> None:
    global _symbols, _timeframes, _show_chart, _limit

    args = parse_args()

    _symbols    = [args.symbol] if args.symbol else config.SYMBOLS
    _timeframes = [args.tf]     if args.tf     else config.TIMEFRAMES
    _show_chart = not args.no_chart
    _limit      = args.limit

    print(BANNER)
    journal.init_db()

    if args.summary:
        journal.print_summary()
        return

    if args.loop:
        log_info("[MAIN] starting hourly scheduler")
        _print_context_table(_symbols, _timeframes)
        run_scheduler(run_cycle, run_on_start=config.RUN_ON_START)
    else:
        _print_context_table(_symbols, _timeframes)
        run_cycle()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nScanner stopped.")
        sys.exit(0)