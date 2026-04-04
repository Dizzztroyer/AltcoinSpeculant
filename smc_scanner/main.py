#!/usr/bin/env python3
"""
main.py — SMC Scanner v2

Usage:
    python main.py                       # run once immediately
    python main.py --loop                # 30-min scheduler aligned to clock
    python main.py --symbol BTC/USDT --tf 15m
    python main.py --no-chart
    python main.py --summary             # DB status counts
    python main.py --dashboard           # HTML portfolio dashboard
    python main.py --report              # print midnight report now
"""

import argparse
import sys

import config
import journal
import portfolio
import evaluator
import scoring
import alerts
from charting import draw_chart
from datafeed import fetch_all
from daily_report import run_daily_report
from liquidity import build_liquidity_zones, detect_sweeps
from orderblocks import find_fvgs, find_order_blocks
from scheduler import run_scheduler
from signals import scan_for_signals
from structure import find_swings, get_market_context
from utils import format_signal, log_error, log_info, log_signal, log_warn


def parse_args():
    p = argparse.ArgumentParser(description="SMC Scanner v2")
    p.add_argument("--symbol",    default=None)
    p.add_argument("--tf",        default=None)
    p.add_argument("--loop",      action="store_true")
    p.add_argument("--no-chart",  action="store_true")
    p.add_argument("--limit",     type=int, default=None)
    p.add_argument("--summary",   action="store_true")
    p.add_argument("--dashboard", action="store_true")
    p.add_argument("--backtest",  action="store_true",
                   help="Run walk-forward backtest and exit")
    p.add_argument("--bt-days",   type=int, default=None,
                   help="Backtest days (default: config.BACKTEST_DAYS)")
    p.add_argument("--bt-symbol", default=None)
    p.add_argument("--bt-tf",     default=None)
    p.add_argument("--report",    action="store_true",
                   help="Print daily report immediately and exit")
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
    """One complete evaluate → scan → score → save → alert pass."""
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
                     f"{sig['direction'].upper()}  score={score}")
            log_signal(format_signal(sig))

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
    portfolio.init_portfolio_db()

    if args.summary:
        journal.print_summary()
        return

    if args.backtest:
        from backtesting import run_backtest, run_full_backtest, \
            print_backtest_report, generate_backtest_html
        import webbrowser
        from pathlib import Path
        days = args.bt_days or getattr(config, "BACKTEST_DAYS", 90)
        syms = [args.bt_symbol] if args.bt_symbol else                [args.symbol]    if args.symbol     else config.SYMBOLS
        tfs  = [args.bt_tf]    if args.bt_tf     else                [args.tf]       if args.tf         else config.TIMEFRAMES
        log_info(f"[BT] Running backtest: {syms} × {tfs} × {days}d")
        results = []
        for sym in syms:
            for tf in tfs:
                results.append(run_backtest(sym, tf, days=days,
                    walk_step=getattr(config, "BACKTEST_WALK_STEP", 3)))
        print_backtest_report(results)
        path = generate_backtest_html(results)
        webbrowser.open(f"file://{Path(path).resolve()}")
        return

    if args.report:
        run_daily_report()
        return

    if args.dashboard:
        from dashboard import print_report, generate_html
        import webbrowser
        from pathlib import Path
        print_report()
        path = generate_html()
        webbrowser.open(f"file://{Path(path).resolve()}")
        return

    if args.loop:
        interval = getattr(config, "SCAN_INTERVAL_MINUTES", 30)
        log_info(f"[MAIN] starting {interval}-minute scheduler "
                 f"(timezone: {config.LOCAL_TIMEZONE})")
        _print_context_table(_symbols, _timeframes)
        run_scheduler(
            run_cycle=run_cycle,
            run_on_start=config.RUN_ON_START,
            interval_minutes=interval,
            daily_report_fn=run_daily_report,
            local_tz=config.LOCAL_TIMEZONE,
        )
    else:
        _print_context_table(_symbols, _timeframes)
        run_cycle()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nScanner stopped.")
        sys.exit(0)