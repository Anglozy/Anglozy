"""Combines the kill-zone filter, news guard and daily loss guard.

``SignalPipeline`` calls:

* :meth:`RiskManager.pre_scan` before looking for a setup: outside a kill
  zone, inside a news blackout, or past the daily loss limit means no scan.
* :meth:`RiskManager.pre_trade` before sending an order: the new trade's own
  risk must still fit under the daily limit, and no blackout may have started.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from config.settings import RiskSettings
from mt5.connector import MT5Connector

from .daily_loss import AccountRisk, DailyLossGuard
from .killzones import KillzoneFilter
from .news import FileCalendar, ForexFactoryFeed, NewsCalendar, NewsGuard

logger = logging.getLogger(__name__)

UTC = dt.timezone.utc
Guard = Literal["killzone", "news", "daily_loss"]


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    guard: Guard | None = None  # which guard blocked
    details: dict = field(default_factory=dict)


class RiskManager:
    def __init__(
        self,
        connector: MT5Connector,
        settings: RiskSettings | None = None,
        killzones: KillzoneFilter | None = None,
        news: NewsGuard | None = None,
        daily: DailyLossGuard | None = None,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(UTC),
    ) -> None:
        self.connector = connector
        self.settings = settings or RiskSettings()
        self.killzones = killzones
        self.news = news
        self.daily = daily
        self.clock = clock

    @classmethod
    def from_settings(
        cls,
        settings: RiskSettings,
        connector: MT5Connector,
        cache_dir: str | Path | None = None,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(UTC),
        calendar_source: Any | None = None,
    ) -> RiskManager:
        killzones = (KillzoneFilter.from_settings(settings.killzones, settings.killzone_timezone)
                     if settings.killzones_enabled else None)
        news = None
        if settings.news_enabled:
            source = calendar_source or (FileCalendar(settings.news_calendar_file) if settings.news_calendar_file
                                         else ForexFactoryFeed(settings.news_calendar_url))
            cache = Path(cache_dir) / "calendar.json" if cache_dir else None
            news = NewsGuard(
                NewsCalendar(source, cache_path=cache, clock=clock),
                currencies=settings.news_currencies,
                impacts=settings.news_impacts,
                before=dt.timedelta(minutes=settings.news_before_minutes),
                after=dt.timedelta(minutes=settings.news_after_minutes),
                fail_closed=settings.news_fail_closed,
            )
        daily = (DailyLossGuard(settings.daily_loss_limit_percent, settings.max_total_loss_percent,
                                settings.initial_balance)
                 if settings.daily_loss_enabled else None)
        return cls(connector, settings, killzones, news, daily, clock)

    # ------------------------------------------------------------ gates
    def pre_scan(self, utc_offset_hours: int) -> RiskDecision:
        now = self.clock()
        if self.killzones:
            status = self.killzones.status(now)
            if not status.inside:
                return RiskDecision(False, self.killzones.describe(status), "killzone")
        if self.news:
            d = self.news.check(now)
            if d.blocked:
                return RiskDecision(False, d.reason, "news")
        if self.daily:
            d = self.daily.check(self.account_risk(utc_offset_hours))
            if d.blocked:
                return RiskDecision(False, d.reason, "daily_loss")
        return RiskDecision(True, "All risk guards passed")

    def pre_trade(
        self, entry: float, stop_loss: float, volume: float, symbol: str, utc_offset_hours: int,
    ) -> RiskDecision:
        """Last check before the order goes out, including the new trade's own risk."""
        if self.news:
            d = self.news.check(self.clock())
            if d.blocked:
                return RiskDecision(False, d.reason, "news")
        if self.daily:
            spec = self.connector.symbol_spec(symbol)
            new_risk = abs(entry - stop_loss) / spec.tick_size * spec.tick_value * volume
            d = self.daily.check(self.account_risk(utc_offset_hours), new_trade_risk=new_risk)
            if d.blocked:
                return RiskDecision(False, d.reason, "daily_loss", {"new_trade_risk": round(new_risk, 2)})
        return RiskDecision(True, "All risk guards passed")

    def order_expiration(self, utc_offset_hours: int) -> dt.datetime | None:
        """End of the current kill zone in broker server time (UTC-labelled, as MT5 expects)."""
        if not (self.killzones and self.settings.expire_orders_at_killzone_end):
            return None
        status = self.killzones.status(self.clock())
        if not status.ends_at:
            return None
        return status.ends_at + dt.timedelta(hours=utc_offset_hours)

    # ------------------------------------------------------------ account
    def day_start(self, utc_offset_hours: int) -> dt.datetime:
        """Start of the broker's trading day, as server wall time in a UTC-aware datetime."""
        server_now = self.clock().astimezone(UTC) + dt.timedelta(hours=utc_offset_hours)
        start = server_now.replace(hour=self.settings.day_reset_hour, minute=0, second=0, microsecond=0)
        if server_now < start:
            start -= dt.timedelta(days=1)
        return start

    def account_risk(self, utc_offset_hours: int) -> AccountRisk:
        account = self.connector.account_info()
        realized = self.connector.realized_pnl_since(self.day_start(utc_offset_hours))
        open_risk = 0.0
        unprotected = []
        specs: dict[str, Any] = {}
        for e in self.connector.exposures():
            if not e.stop_loss:
                unprotected.append(e.ticket)
                continue
            spec = specs.get(e.symbol) or specs.setdefault(e.symbol, self.connector.symbol_spec(e.symbol))
            distance = (e.price - e.stop_loss) if e.side == "BUY" else (e.stop_loss - e.price)
            open_risk += max(distance, 0.0) / spec.tick_size * spec.tick_value * e.volume
        return AccountRisk(account.balance, account.equity, realized, round(open_risk, 2),
                           tuple(unprotected), account.currency)

    # ------------------------------------------------------------ dashboard
    def snapshot(self, utc_offset_hours: int) -> dict[str, Any]:
        """Current state of every guard, for the dashboard. Never raises."""
        now = self.clock()
        out: dict[str, Any] = {"checked_at": now.isoformat()}

        if self.killzones:
            st = self.killzones.status(now)
            out["killzone"] = {
                "enabled": True, "inside": st.inside, "active": st.active.name if st.active else None,
                "ends_at": st.ends_at.isoformat() if st.ends_at else None,
                "next": st.next_zone.name if st.next_zone else None,
                "next_start": st.next_start.isoformat() if st.next_start else None,
                "timezone": self.killzones.timezone, "message": self.killzones.describe(st),
            }
        else:
            out["killzone"] = {"enabled": False}

        if self.news:
            d = self.news.check(now)
            ev = d.next_event
            out["news"] = {
                "enabled": True, "blocked": d.blocked, "message": d.reason,
                "event": _event(d.event), "next_event": _event(ev),
                "calendar_updated": self.news.calendar.fetched_at.isoformat() if self.news.calendar.fetched_at else None,
            }
        else:
            out["news"] = {"enabled": False}

        if self.daily:
            try:
                d = self.daily.check(self.account_risk(utc_offset_hours))
                out["daily_loss"] = {
                    "enabled": True, "blocked": d.blocked, "message": d.reason,
                    "day_start_balance": round(d.day_start_balance, 2), "day_loss": round(d.day_loss, 2),
                    "limit_amount": round(d.limit_amount, 2), "limit_percent": self.daily.limit_percent,
                    "used_percent": round(d.used_percent, 2), "worst_case_loss": round(d.worst_case_loss, 2),
                }
            except Exception as exc:  # dashboard info only
                out["daily_loss"] = {"enabled": True, "blocked": None, "message": f"Unavailable: {exc}"}
        else:
            out["daily_loss"] = {"enabled": False}
        return out


def _event(e) -> dict[str, Any] | None:
    if e is None:
        return None
    return {"title": e.title, "currency": e.currency, "impact": e.impact, "time": e.time.isoformat()}
