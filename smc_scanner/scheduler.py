# scheduler.py — Precise hourly scheduler
#
# Aligns execution to real wall-clock hours (10:00, 11:00, 12:00 ...).
# Never drifts regardless of how long run_cycle() takes.

import time
from datetime import datetime, timezone, timedelta
from typing import Callable

from utils import log_info


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _next_full_hour(from_dt: datetime | None = None) -> datetime:
    """
    Return the next UTC datetime where minute=0, second=0, microsecond=0.

    Example:
        10:23:45  →  11:00:00
        10:00:00  →  11:00:00  (exact boundary → still advances one hour)
    """
    dt = from_dt or _now_utc()
    # Truncate to current hour, then add one full hour
    top_of_hour = dt.replace(minute=0, second=0, microsecond=0)
    return top_of_hour + timedelta(hours=1)


def wait_until_next_hour() -> None:
    """
    Sleep precisely until the next full UTC hour.
    Re-checks the target time every second to avoid over-sleeping
    if the system clock is adjusted.
    """
    target = _next_full_hour()
    log_info(f"[SCHEDULER] sleeping until {target.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    while True:
        remaining = (target - _now_utc()).total_seconds()
        if remaining <= 0:
            break
        # Sleep in short chunks so we stay accurate even near the boundary
        time.sleep(min(remaining, 30))

    log_info(f"[SCHEDULER] woke up at {_now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC")


def run_scheduler(run_cycle: Callable[[], None],
                  run_on_start: bool = True) -> None:
    """
    Infinite scheduler loop.

    Parameters
    ----------
    run_cycle    : the function to call each hour
    run_on_start : if True, run immediately on startup before waiting;
                   if False, wait until the next full hour first
    """
    if not run_on_start:
        log_info("[SCHEDULER] RUN_ON_START=False — waiting for next full hour")
        wait_until_next_hour()

    while True:
        start = _now_utc()
        log_info(f"[SCHEDULER] cycle started at "
                 f"{start.strftime('%Y-%m-%d %H:%M:%S')} UTC")

        try:
            run_cycle()
        except Exception as exc:
            # Never let a crash kill the scheduler loop
            from utils import log_error
            log_error(f"[SCHEDULER] run_cycle raised an exception: {exc}")

        finish = _now_utc()
        elapsed = round((finish - start).total_seconds(), 1)
        log_info(f"[SCHEDULER] cycle finished in {elapsed}s")

        wait_until_next_hour()