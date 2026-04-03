#!/usr/bin/env python3
"""main.py — SMC Scanner v2"""

import argparse
import sys

import config
import journal
import evaluator
import scoring
import alerts
from charting import draw_chart, render_chart_image
from datafeed import fetch_all
from liquidity import build_liquidity_zones, detect_sweeps
from orderblocks import find_fvgs, find_order_blocks
from scheduler import run_scheduler
from signals import scan_for_signals
from structure import find_swings, get_market_context
from utils import format_signal, log_error, log_info, log_signal, log_warn


def parse_args():
    p = argparse.ArgumentParser(description="SMC Scanner v2")
    p.add_argument("--symbol",   default=None)
    p.add_argument("--tf",       default=None)
    p.add_argument("--loop",     action="store_true")
    p.add_argument("--no-chart", action="store_true")
    p.add_argument("--limit",    type=int, default=None)
    p.add_argument("--summary",  action="store_true")
    return p.parse_args()


def _print_context_table(symbols, timeframes):
    data = fetch_all(symbols=symbols, timeframes=timeframes)
    print()
    print(f"  {'Symbol':<14} {'TF':<8} {'Context':<12} Candles")
    print("  " + "-" * 46)
    for (sym, tf), df in data.items():
        if df.empty:
            continue
        print(f"  {sym:<14} {tf:<8} "
              f"{get_market_context(find_swings(df)):<12} {len(df)}")
    print()


_symbols:    list[str]  = []
_timeframes: list[str]  = []
_show_chart: bool       = False
_limit:      int | None = None


def run_cycle() -> None:
    evaluator.evaluate_open_signals()

    data = fetch_all(symbols=_symbols, timeframes=_timeframes)

    for (sym, tf), df in data.items():
        if df.empty:
            log_warn(f"[SCAN] skipping {sym} {tf}: empty")
            continue
        if _limit:
            df = df.tail(_limit)

        log_info(f"[SCAN] {sym} [{tf}]  ({len(df)} candles)")

        try:
            raw_signals = scan_for_signals(sym, tf, df)
        except Exception as exc:
            log_error(f"[SCAN] error {sym} {tf}: {exc}")
            continue

        if not raw_signals:
            log_info("[SCAN]   no setup found")
            continue

        # Pre-compute chart data once per (sym, tf)
        df_s   = find_swings(df)
        zones  = build_liquidity_zones(df_s)
        sweeps = detect_sweeps(df_s, zones)
        fvgs   = find_fvgs(df_s)
        obs    = find_order_blocks(df_s, fvgs=fvgs)

        for sig in raw_signals:

            try:
                score, htf_bias = scoring.score_signal(sig, df)
            except Exception as exc:
                log_warn(f"[SCORE] error: {exc}")
                score, htf_bias = 40, ""

            vol_ok = (scoring._volume_confirmed(df)
                      if config.ENABLE_VOLUME_CONFIRMATION else False)

            signal_id = journal.save_signal(sig, score=score,
                                            higher_tf_bias=htf_bias)
            if signal_id is None:
                log_info(f"[DB] duplicate — {sym} {tf} {sig['direction'].upper()}")
                continue

            log_info(f"[DB] saved #{signal_id}  {sym} {tf} "
                     f"{sig['direction'].upper()}  score={score}  "
                     f"OB={'yes' if sig.get('ob_label') else 'no'}  "
                     f"FVG={'yes' if sig.get('ob_has_fvg') else 'no'}")
            log_signal(format_signal(sig))

            # Alert with chart image (pass obs/fvgs for full annotations)
            alerts.maybe_send_alert(
                signal_id, sig, score, htf_bias, vol_ok,
                df=df_s, zones=zones, sweeps=sweeps,
                obs=obs, fvgs=fvgs,
            )

            if _show_chart and config.SHOW_CHART:
                try:
                    draw_chart(df_s, sym, tf, zones, sweeps, sig, obs, fvgs)
                except Exception as exc:
                    log_warn(f"[CHART] error: {exc}")

    journal.print_summary()


BANNER = r"""
  ╔══════════════════════════════════════════════════╗
  ║   SMC CRYPTO SCANNER  v2  — signal engine        ║
  ║   OB · FVG · Sweep · BOS · Score · Alerts        ║
  ╚══════════════════════════════════════════════════╝
"""


def main():
    global _symbols, _timeframes, _show_chart, _limit
    args        = parse_args()
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