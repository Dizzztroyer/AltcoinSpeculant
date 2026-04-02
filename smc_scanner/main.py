#!/usr/bin/env python3
"""
main.py — SMC Scanner v2 entry point

Each scan cycle:
  1. init DB
  2. evaluate old open signals
  3. scan market for fresh signals
  4. score each fresh signal
  5. save to DB
  6. send Telegram alert (if score qualifies)
  7. print readable console summary

Usage:
    python main.py                          # one-shot scan
    python main.py --loop                   # hourly loop
    python main.py --symbol BTC/USDT --tf 15m
    python main.py --no-chart
    python main.py --summary                # show DB summary only
"""

import argparse
import sys
import time

import config
import journal
import evaluator
import scoring
import alerts
from charting import draw_chart
from datafeed import fetch_all, fetch_ohlcv
from liquidity import build_liquidity_zones, detect_sweeps
from signals import scan_for_signals
from structure import find_swings, get_market_context
from utils import format_signal, log_error, log_info, log_signal, log_warn


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SMC Scanner v2")
    p.add_argument("--symbol",   default=None)
    p.add_argument("--tf",       default=None)
    p.add_argument("--loop",     action="store_true")
    p.add_argument("--no-chart", action="store_true")
    p.add_argument("--limit",    type=int, default=None)
    p.add_argument("--summary",  action="store_true",
                   help="Print DB signal summary and exit")
    return p.parse_args()


# ── Single scan pass ──────────────────────────────────────────────────────────

def run_cycle(symbols: list[str],
              timeframes: list[str],
              show_chart: bool = False,
              limit: int | None = None) -> int:
    """
    Run one complete scan-evaluate-score-alert cycle.
    Returns count of new signals generated.
    """

    # Step 1 — Evaluate previously open signals
    evaluator.evaluate_open_signals()

    # Step 2 — Fetch fresh data and scan
    data = fetch_all(symbols=symbols, timeframes=timeframes)
    new_signals_count = 0

    for (sym, tf), df in data.items():
        if df.empty:
            log_warn(f"[SCAN] skipping {sym} {tf}: empty data")
            continue

        if limit:
            df = df.tail(limit)

        log_info(f"[SCAN] {sym} [{tf}]  ({len(df)} candles)")

        try:
            raw_signals = scan_for_signals(sym, tf, df)
        except Exception as exc:
            log_error(f"[SCAN] error on {sym} {tf}: {exc}")
            continue

        if not raw_signals:
            log_info(f"[SCAN]   no setup found")
            continue

        for sig in raw_signals:
            # Step 3 — Score
            try:
                score, htf_bias = scoring.score_signal(sig, df)
            except Exception as exc:
                log_warn(f"[SCORE] error: {exc}")
                score, htf_bias = 40, ""

            # Volume flag (for Telegram message)
            vol_ok = scoring._volume_confirmed(df) if config.ENABLE_VOLUME_CONFIRMATION else False

            # Step 4 — Save to DB
            signal_id = journal.save_signal(sig, score=score, higher_tf_bias=htf_bias)
            if signal_id is None:
                log_info(f"[DB] duplicate signal ignored for {sym} {tf}")
                continue

            new_signals_count += 1
            log_info(f"[DB] saved signal #{signal_id}  {sym} {tf} "
                     f"{sig['direction'].upper()}  score={score}")

            # Step 5 — Print to console
            text = format_signal(sig)
            log_signal(text)

            # Step 6 — Telegram alert
            alerts.maybe_send_alert(signal_id, sig, score, htf_bias, vol_ok)

            # Step 7 — Optional chart
            if show_chart and config.SHOW_CHART:
                try:
                    df_s   = find_swings(df)
                    zones  = build_liquidity_zones(df_s)
                    sweeps = detect_sweeps(df_s, zones)
                    draw_chart(df_s, sym, tf, zones, sweeps, sig)
                except Exception as exc:
                    log_warn(f"[CHART] error: {exc}")

    return new_signals_count


# ── Context summary ───────────────────────────────────────────────────────────

def print_context_table(symbols: list[str], timeframes: list[str]) -> None:
    data = fetch_all(symbols=symbols, timeframes=timeframes)
    print()
    print(f"  {'Symbol':<14} {'TF':<8} {'Context':<12} {'Candles'}")
    print("  " + "-" * 48)
    for (sym, tf), df in data.items():
        if df.empty:
            continue
        df_s    = find_swings(df)
        context = get_market_context(df_s)
        print(f"  {sym:<14} {tf:<8} {context:<12} {len(df)}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

BANNER = r"""
  ╔══════════════════════════════════════════════════╗
  ║   SMC CRYPTO SCANNER  v2  — signal engine        ║
  ║   Sweep · BOS · Score · Journal · Alerts         ║
  ╚══════════════════════════════════════════════════╝
"""

def main() -> None:
    args = parse_args()

    symbols    = [args.symbol] if args.symbol else config.SYMBOLS
    timeframes = [args.tf]     if args.tf     else config.TIMEFRAMES
    show_chart = not args.no_chart

    print(BANNER)

    # Always initialise the DB first
    journal.init_db()

    if args.summary:
        journal.print_summary()
        return

    if args.loop:
        log_info(f"[LOOP] starting — interval={config.SCAN_INTERVAL_SECONDS}s")
        iteration = 0
        while True:
            iteration += 1
            log_info(f"\n{'─'*60}")
            log_info(f"[LOOP] cycle #{iteration}")
            log_info(f"{'─'*60}")
            n = run_cycle(symbols, timeframes,
                          show_chart=show_chart, limit=args.limit)
            journal.print_summary()
            log_info(f"[LOOP] new signals this cycle: {n} — "
                     f"sleeping {config.SCAN_INTERVAL_SECONDS}s ...")
            time.sleep(config.SCAN_INTERVAL_SECONDS)
    else:
        print_context_table(symbols, timeframes)
        n = run_cycle(symbols, timeframes,
                      show_chart=show_chart, limit=args.limit)
        journal.print_summary()
        if n == 0:
            log_info("[SCAN] no trade setups detected this pass")
        else:
            log_info(f"[SCAN] total new signals: {n}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nScanner stopped by user.")
        sys.exit(0)