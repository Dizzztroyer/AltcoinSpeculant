# killzones.py — Trading session / kill zone filter
#
# Kill zones = windows of high institutional activity where SMC setups
# have the highest probability of follow-through.
#
# Sessions (all UTC):
#   Asian Kill Zone    : 00:00 – 04:00
#   London Open KZ     : 07:00 – 09:00   ← highest volume
#   New York Open KZ   : 12:00 – 14:00   ← highest volume
#   London Close KZ    : 15:00 – 16:00
#   New York PM        : 19:00 – 20:00
#
# Mode (config.KILLZONE_MODE):
#   "log"    — always allow trade, but log whether in KZ or not (current)
#   "filter" — reject signals outside any KZ (strict)
#   "score"  — only affect scoring, no hard block
#   "off"    — completely disabled
#
# This allows gradual rollout: run in "log" mode first to see
# how many signals fall inside vs outside KZ, then switch to "filter".

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, time
from typing import Literal

import config
from utils import log_info


# ── Kill zone definitions ──────────────────────────────────────────────────────

@dataclass
class KillZone:
    name:       str
    start_utc:  time    # start time in UTC
    end_utc:    time    # end time in UTC
    quality:    int     # 1-3, higher = more important

    def is_active(self, dt_utc: datetime) -> bool:
        t = dt_utc.time().replace(second=0, microsecond=0)
        if self.start_utc <= self.end_utc:
            return self.start_utc <= t < self.end_utc
        else:
            # Wraps midnight (e.g. 23:00–01:00)
            return t >= self.start_utc or t < self.end_utc


KILL_ZONES: list[KillZone] = [
    KillZone("Asian KZ",       time(0,  0), time(4,  0), quality=1),
    KillZone("London Open KZ", time(7,  0), time(9,  0), quality=3),
    KillZone("New York KZ",    time(12, 0), time(14, 0), quality=3),
    KillZone("London Close",   time(15, 0), time(16, 0), quality=2),
    KillZone("NY PM",          time(19, 0), time(20, 0), quality=1),
]


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class KZResult:
    in_killzone:   bool
    zone_name:     str   # name of active KZ, or "Outside KZ"
    zone_quality:  int   # 1-3 or 0 if outside
    allowed:       bool  # depends on KILLZONE_MODE


def check_killzone(dt_utc: datetime | None = None) -> KZResult:
    """
    Check whether the current UTC time falls inside a kill zone.

    Returns KZResult with in_killzone, zone_name, zone_quality, allowed.
    """
    mode = getattr(config, "KILLZONE_MODE", "log")

    if mode == "off":
        return KZResult(in_killzone=True, zone_name="KZ disabled",
                        zone_quality=3, allowed=True)

    now = dt_utc or datetime.now(timezone.utc)

    for kz in KILL_ZONES:
        if kz.is_active(now):
            result = KZResult(
                in_killzone=True,
                zone_name=kz.name,
                zone_quality=kz.quality,
                allowed=True,   # always allowed when inside KZ
            )
            log_info(f"[KZ] ✅ inside {kz.name} (quality={kz.quality}/3)")
            return result

    # Outside all kill zones
    allowed = mode != "filter"   # filter mode blocks; log/score mode allows
    result  = KZResult(
        in_killzone=False,
        zone_name="Outside KZ",
        zone_quality=0,
        allowed=allowed,
    )

    if mode == "filter":
        log_info(f"[KZ] ❌ {now.strftime('%H:%M')} UTC — outside all kill zones — BLOCKED")
    else:
        log_info(f"[KZ] ⚪ {now.strftime('%H:%M')} UTC — outside kill zones "
                 f"(mode={mode}, signal still logged)")
    return result


def kz_score_bonus(result: KZResult) -> int:
    """
    Return a score adjustment based on KZ quality.
    Used by scoring.py when KILLZONE_MODE = 'score'.
    """
    if not result.in_killzone:
        return -5        # slight penalty for out-of-KZ
    return {1: 0, 2: 5, 3: 10}.get(result.zone_quality, 0)


def active_zones_str() -> str:
    """Human-readable list of all KZ windows."""
    lines = []
    for kz in KILL_ZONES:
        lines.append(f"  {kz.name:<20} {kz.start_utc.strftime('%H:%M')}–"
                     f"{kz.end_utc.strftime('%H:%M')} UTC  ★{'★'*(kz.quality-1)}")
    return "\n".join(lines)