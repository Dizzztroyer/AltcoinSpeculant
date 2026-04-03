# scheduler.py — Precise interval scheduler with daily report trigger
#
# Runs scan cycle every SCAN_INTERVAL_MINUTES (default 30).
# Aligns to real clock boundaries: :00 and :30 (or :00/:20/:40 for 20m, etc.)
# At midnight LOCAL time → fires daily_report_fn() before the regular cycle.

import time
from datetime import datetime, timezone, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from utils import log_info, log_error


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_local(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:
        return datetime.now(timezone.utc)


def _next_interval(interval_minutes: int,
                   from_dt: datetime | None = None) -> datetime:
    """
    Return the next UTC datetime aligned to the interval boundary.

    Examples (interval=30):
        10:07  →  10:30
        10:30  →  11:00
        10:31  →  11:00

    Examples (interval=60):
        10:23  →  11:00
        11:00  →  12:00
    """
    dt  = from_dt or _now_utc()
    dt  = dt.replace(second=0, microsecond=0)

    # How many full intervals have elapsed since midnight?
    minutes_since_midnight = dt.hour * 60 + dt.minute
    intervals_done = minutes_since_midnight // interval_minutes

    # Advance to the start of the NEXT interval
    next_start_minutes = (intervals_done + 1) * interval_minutes

    midnight = dt.replace(hour=0, minute=0)
    target   = midnight + timedelta(minutes=next_start_minutes)

    # Rare edge: rolled past midnight
    if target.date() > dt.date():
        target = target   # that's fine, it's the next day's 00:00

    return target


def wait_until_next_interval(interval_minutes: int) -> None:
    """Sleep until the next aligned interval boundary."""
    target = _next_interval(interval_minutes)
    log_info(f"[SCHEDULER] sleeping until "
             f"{target.strftime('%Y-%m-%d %H:%M:%S')} UTC  "
             f"(interval={interval_minutes}m)")

    while True:
        remaining = (target - _now_utc()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 15))   # wake up often near boundary

    log_info(f"[SCHEDULER] woke up at "
             f"{_now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC")


def run_scheduler(run_cycle: Callable[[], None],
                  run_on_start: bool = True,
                  interval_minutes: int = 30,
                  daily_report_fn: Callable[[], None] | None = None,
                  local_tz: str = "UTC") -> None:
    """
    Infinite scheduler loop.

    Parameters
    ----------
    run_cycle        : main scan function (called every interval)
    run_on_start     : if True, run immediately on startup then align
    interval_minutes : scan interval in minutes (default 30)
    daily_report_fn  : called at local midnight before the regular cycle
    local_tz         : IANA timezone string for midnight detection
                       (e.g. 'Europe/Kiev', 'America/New_York')
    """
    if not run_on_start:
        log_info("[SCHEDULER] RUN_ON_START=False — waiting for next interval")
        wait_until_next_interval(interval_minutes)

    last_report_date: str | None = None

    while True:
        start      = _now_utc()
        local_now  = _now_local(local_tz)
        today_str  = local_now.strftime("%Y-%m-%d")

        log_info(f"[SCHEDULER] ── cycle #{_cycle_counter()} "
                 f"started {start.strftime('%Y-%m-%d %H:%M:%S')} UTC "
                 f"/ {local_now.strftime('%H:%M')} {local_tz} ──")

        # ── Daily report at local midnight ────────────────────────────────────
        is_midnight_window = local_now.hour == 0 and local_now.minute < interval_minutes
        if (daily_report_fn is not None
                and is_midnight_window
                and last_report_date != today_str):
            log_info("[SCHEDULER] midnight detected — firing daily report")
            try:
                daily_report_fn()
            except Exception as exc:
                log_error(f"[SCHEDULER] daily report error: {exc}")
            last_report_date = today_str

        # ── Regular scan cycle ────────────────────────────────────────────────
        try:
            run_cycle()
        except Exception as exc:
            log_error(f"[SCHEDULER] run_cycle raised: {exc}")

        finish  = _now_utc()
        elapsed = round((finish - start).total_seconds(), 1)
        log_info(f"[SCHEDULER] cycle finished in {elapsed}s")

        wait_until_next_interval(interval_minutes)


# ── Simple counter ─────────────────────────────────────────────────────────────
_counter = 0

def _cycle_counter() -> int:
    global _counter
    _counter += 1
    return _counter