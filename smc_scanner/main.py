#!/usr/bin/env python3
"""
main.py — SMC Scanner entry point

Usage:
    python main.py                    # one-shot scan
    python main.py --loop             # continuous loop (every SCAN_INTERVAL_SECONDS)
    python main.py --symbol BTC/USDT --tf 15m   # single pair / timeframe
    python main.py --no-chart         # suppress Plotly charts
"""

import argparse
import sys
import time

import config
from charting import draw_chart
from datafeed import fetch_all, fetch_ohlcv
from liquidity import build_liquidity_zones, detect_sweeps
from signals import scan_for_signals
from structure import find_swings, get_market_context
from utils import format_signal, log_error, log_info, log_signal, log_warn


# ── CLI args ───────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SMC Crypto Scanner")
    p.add_argument("--symbol",   default=None, help="Override symbol, e.g. BTC/USDT")
    p.add_argument("--tf",       default=None, help="Override timeframe, e.g. 15m")
    p.add_argument("--loop",     action="store_true", help="Run continuously")
    p.add_argument("--no-chart", action="store_true", help="Disable Plotly charts")
    p.add_argument("--limit",    type=int, default=None, help="Candle count override")
    return p.parse_args()


# ── Core scan logic ────────────────────────────────────────────────────────────

def run_scan(symbols: list[str],
             timeframes: list[str],
             show_chart: bool = True,
             limit: int | None = None) -> int:
    """
    Fetch data, run the full pipeline, print signals.
    Returns the number of signals found.
    """
    data = fetch_all(symbols=symbols, timeframes=timeframes)
    total_signals = 0

    for (sym, tf), df in data.items():
        if df.empty:
            log_warn(f"Skipping {sym} {tf}: empty data")
            continue

        if limit:
            df = df.tail(limit)

        log_info(f"Analysing {sym} [{tf}]  ({len(df)} candles) ...")

        try:
            signals = scan_for_signals(sym, tf, df)
        except Exception as exc:
            log_error(f"Error scanning {sym} {tf}: {exc}")
            continue

        if not signals:
            log_info(f"  No setup found for {sym} [{tf}]")
            continue

        for sig in signals:
            total_signals += 1
            text = format_signal(sig)
            log_signal(text)

            if show_chart and config.SHOW_CHART:
                try:
                    df_s   = find_swings(df)
                    zones  = build_liquidity_zones(df_s)
                    sweeps = detect_sweeps(df_s, zones)
                    draw_chart(df_s, sym, tf, zones, sweeps, sig)
                except Exception as exc:
                    log_warn(f"Chart error: {exc}")

    return total_signals


# ── Summary of market context (no-signal run) ──────────────────────────────────

def print_context_summary(symbols: list[str], timeframes: list[str]) -> None:
    """Quick context print for every pair — useful for monitoring."""
    data = fetch_all(symbols=symbols, timeframes=timeframes)
    print()
    print("  {:<14} {:<8} {:<12} {:<10}".format("Symbol", "TF", "Context", "Candles"))
    print("  " + "-" * 50)
    for (sym, tf), df in data.items():
        if df.empty:
            continue
        df_s    = find_swings(df)
        context = get_market_context(df_s)
        print("  {:<14} {:<8} {:<12} {:<10}".format(sym, tf, context, len(df)))
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    symbols    = [args.symbol]    if args.symbol else config.SYMBOLS
    timeframes = [args.tf]        if args.tf     else config.TIMEFRAMES
    show_chart = not args.no_chart

    banner = r"""
  ╔═══════════════════════════════════════════════╗
  ║     SMC CRYPTO SCANNER  — Dizzztroyer         ║
  ║   Liquidity · Structure · Sweep · BOS/MBOS    ║
  ╚═══════════════════════════════════════════════╝
    """
    print(banner)

    if args.loop:
        log_info(f"Starting continuous loop (interval={config.SCAN_INTERVAL_SECONDS}s)")
        iteration = 0
        while True:
            iteration += 1
            log_info(f"── Scan #{iteration} ──────────────────────────────────")
            n = run_scan(symbols, timeframes, show_chart=show_chart, limit=args.limit)
            log_info(f"Signals found this pass: {n}")
            log_info(f"Sleeping {config.SCAN_INTERVAL_SECONDS}s …")
            time.sleep(config.SCAN_INTERVAL_SECONDS)
    else:
        print_context_summary(symbols, timeframes)
        n = run_scan(symbols, timeframes, show_chart=show_chart, limit=args.limit)
        if n == 0:
            log_info("No trade setups detected in this scan.")
        else:
            log_info(f"Total signals generated: {n}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nScanner stopped by user.")
        sys.exit(0)