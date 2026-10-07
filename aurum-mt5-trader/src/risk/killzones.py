"""Kill-zone filter: only evaluate setups inside configured session windows.

Windows are wall-clock times in one IANA timezone. The default,
``Africa/Nairobi``, is a fixed UTC+3 with no daylight saving, so the windows
never drift. A broker server that moves between UTC+3 and UTC+2 with US
daylight saving does not affect them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc


@dataclass(frozen=True)
class Killzone:
    name: str
    start: dt.time
    end: dt.time

    @classmethod
    def parse(cls, name: str, start: str, end: str) -> Killzone:
        s, e = dt.time.fromisoformat(start), dt.time.fromisoformat(end)
        if not s < e:
            raise ValueError(f"kill zone {name!r}: start {start} must be before end {end} (no midnight wrap)")
        return cls(name, s, e)


@dataclass(frozen=True)
class KillzoneStatus:
    active: Killzone | None
    ends_at: dt.datetime | None  # UTC end of the active window
    next_zone: Killzone | None
    next_start: dt.datetime | None  # UTC start of the next window

    @property
    def inside(self) -> bool:
        return self.active is not None


class KillzoneFilter:
    def __init__(self, zones: list[Killzone], timezone: str = "Africa/Nairobi") -> None:
        if not zones:
            raise ValueError("at least one kill zone is required")
        self.zones = sorted(zones, key=lambda z: z.start)
        self.tz = ZoneInfo(timezone)
        self.timezone = timezone

    @classmethod
    def from_settings(cls, zones: tuple[tuple[str, str, str], ...], timezone: str) -> KillzoneFilter:
        return cls([Killzone.parse(*z) for z in zones], timezone)

    def status(self, now: dt.datetime) -> KillzoneStatus:
        local = now.astimezone(self.tz)
        active = ends = None
        for z in self.zones:
            start = dt.datetime.combine(local.date(), z.start, self.tz)
            end = dt.datetime.combine(local.date(), z.end, self.tz)
            if start <= local < end:
                active, ends = z, end.astimezone(UTC)
                break

        nxt = nxt_start = None
        for day in range(0, 8):
            date = local.date() + dt.timedelta(days=day)
            if date.weekday() >= 5:  # markets closed on Saturday/Sunday
                continue
            for z in self.zones:
                start = dt.datetime.combine(date, z.start, self.tz)
                if start > local:
                    nxt, nxt_start = z, start.astimezone(UTC)
                    break
            if nxt:
                break
        return KillzoneStatus(active, ends, nxt, nxt_start)

    def describe(self, status: KillzoneStatus) -> str:
        if status.active:
            return f"Inside {status.active.name} kill zone until {self._local(status.ends_at)}"
        if status.next_zone:
            return f"Outside kill zones; next is {status.next_zone.name} at {self._local(status.next_start)}"
        return "Outside kill zones"

    def _local(self, when: dt.datetime) -> str:
        return when.astimezone(self.tz).strftime("%a %H:%M") + f" ({self.timezone})"
