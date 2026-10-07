"""News guard: block new trades around high-impact economic releases.

Events come from ForexFactory's weekly calendar export (JSON, published at
``nfs.faireconomy.media``), which needs no browser and isn't behind the
site's bot challenge. The feed is fetched at most every ``refresh`` interval
and cached to disk, so restarts don't hammer it.

If the calendar can't be loaded and no recent cache exists, the guard fails
closed by default: no new trades until news risk can be checked again.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

UTC = dt.timezone.utc


class CalendarUnavailable(RuntimeError):
    """No usable calendar data (fetch failed and no fresh cache)."""


@dataclass(frozen=True)
class NewsEvent:
    title: str
    currency: str
    impact: str  # High / Medium / Low / Holiday
    time: dt.datetime  # timezone-aware, UTC
    forecast: str = ""
    previous: str = ""


def parse_ff_json(payload: Any) -> list[NewsEvent]:
    """Parse ForexFactory's calendar export: a list of
    ``{"title", "country", "date" (ISO 8601 with offset), "impact", "forecast", "previous"}``."""
    if not isinstance(payload, list):
        raise ValueError("calendar JSON must be a list of events")
    events = []
    for item in payload:
        try:
            when = dt.datetime.fromisoformat(str(item["date"]))
        except (KeyError, ValueError):
            logger.debug("Skipping calendar row without a valid date: %r", item)
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        events.append(NewsEvent(
            title=str(item.get("title", "")).strip(),
            currency=str(item.get("country", "")).strip().upper(),
            impact=str(item.get("impact", "")).strip().capitalize(),
            time=when.astimezone(UTC),
            forecast=str(item.get("forecast", "") or ""),
            previous=str(item.get("previous", "") or ""),
        ))
    return sorted(events, key=lambda e: e.time)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
class CalendarSource(Protocol):
    def fetch(self) -> list[NewsEvent]: ...


class ForexFactoryFeed:
    """Downloads the weekly calendar export over HTTPS."""

    def __init__(self, url: str, timeout: float = 15.0) -> None:
        self.url = url
        self.timeout = timeout

    def fetch(self) -> list[NewsEvent]:
        req = urllib.request.Request(self.url, headers={"User-Agent": "aurum-mt5-trader/1.0 (news guard)"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed https URL
            return parse_ff_json(json.loads(resp.read().decode("utf-8")))


class FileCalendar:
    """Reads a calendar JSON file in the same format (manual override / offline use)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch(self) -> list[NewsEvent]:
        return parse_ff_json(json.loads(self.path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# Cached calendar
# --------------------------------------------------------------------------- #
class NewsCalendar:
    """Serves events from memory/disk, refreshing from ``source`` when stale."""

    def __init__(
        self,
        source: CalendarSource,
        cache_path: str | Path | None = None,
        refresh: dt.timedelta = dt.timedelta(hours=6),
        max_stale: dt.timedelta = dt.timedelta(hours=24),
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(UTC),
    ) -> None:
        self.source = source
        self.cache_path = Path(cache_path) if cache_path else None
        self.refresh = refresh
        self.max_stale = max_stale
        self.clock = clock
        self._events: list[NewsEvent] | None = None
        self._fetched_at: dt.datetime | None = None
        self.last_error: str | None = None
        self._load_cache()

    def events(self) -> list[NewsEvent]:
        now = self.clock()
        if self._events is not None and self._fetched_at and now - self._fetched_at < self.refresh:
            return self._events
        try:
            events = self.source.fetch()
        except Exception as exc:  # network, HTTP 429, bad JSON...
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self._events is not None and self._fetched_at and now - self._fetched_at < self.max_stale:
                logger.warning("Calendar refresh failed (%s); using data from %s", self.last_error, self._fetched_at)
                return self._events
            raise CalendarUnavailable(f"economic calendar unavailable ({self.last_error})") from exc
        self._events, self._fetched_at, self.last_error = events, now, None
        self._save_cache()
        logger.info("Economic calendar refreshed: %d events", len(events))
        return events

    @property
    def fetched_at(self) -> dt.datetime | None:
        return self._fetched_at

    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.is_file():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._fetched_at = dt.datetime.fromisoformat(data["fetched_at"])
            self._events = parse_ff_json(data["events"])
        except Exception as exc:
            logger.warning("Ignoring unreadable calendar cache %s: %s", self.cache_path, exc)
            self._events = self._fetched_at = None

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_at": self._fetched_at.isoformat(),
                "events": [
                    {"title": e.title, "country": e.currency, "date": e.time.isoformat(), "impact": e.impact,
                     "forecast": e.forecast, "previous": e.previous}
                    for e in self._events
                ],
            }
            self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write calendar cache: %s", exc)


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NewsDecision:
    blocked: bool
    reason: str
    event: NewsEvent | None = None  # the event causing the blackout
    next_event: NewsEvent | None = None  # next relevant event after now


class NewsGuard:
    def __init__(
        self,
        calendar: NewsCalendar,
        currencies: tuple[str, ...] = ("USD",),
        impacts: tuple[str, ...] = ("High",),
        before: dt.timedelta = dt.timedelta(minutes=30),
        after: dt.timedelta = dt.timedelta(minutes=30),
        fail_closed: bool = True,
    ) -> None:
        self.calendar = calendar
        self.currencies = {c.upper() for c in currencies}
        self.impacts = {i.capitalize() for i in impacts}
        self.before = before
        self.after = after
        self.fail_closed = fail_closed

    def relevant(self, events: list[NewsEvent]) -> list[NewsEvent]:
        return [e for e in events if e.currency in self.currencies and e.impact in self.impacts]

    def check(self, now: dt.datetime) -> NewsDecision:
        try:
            events = self.relevant(self.calendar.events())
        except CalendarUnavailable as exc:
            if self.fail_closed:
                return NewsDecision(True, f"News risk unknown: {exc}. No new trades until the calendar loads.")
            return NewsDecision(False, f"News check skipped: {exc}")

        upcoming = next((e for e in events if e.time > now), None)
        for e in events:
            if e.time - self.before <= now <= e.time + self.after:
                local = e.time.strftime("%H:%M UTC")
                return NewsDecision(
                    True,
                    f"News blackout: {e.currency} {e.impact.lower()}-impact '{e.title}' at {local} "
                    f"(no new trades {int(self.before.total_seconds() // 60)} min before to "
                    f"{int(self.after.total_seconds() // 60)} min after)",
                    event=e, next_event=upcoming,
                )
        return NewsDecision(False, "No high-impact news in window", next_event=upcoming)
