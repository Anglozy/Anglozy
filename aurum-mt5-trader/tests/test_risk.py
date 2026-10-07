import datetime as dt
import json
from types import SimpleNamespace

import pytest

from config.settings import RiskSettings, TradingSettings, load_settings
from config.strategy import StrategyParameters
from mt5.connector import MT5Connector
from mt5.executor import MT5Executor
from aurum.generator import AurumReportGenerator
from risk.daily_loss import AccountRisk, DailyLossGuard
from risk.killzones import Killzone, KillzoneFilter
from risk.manager import RiskManager
from risk.news import (
    CalendarUnavailable,
    FileCalendar,
    ForexFactoryFeed,
    NewsCalendar,
    NewsGuard,
    parse_ff_json,
)
from signals.parser import SignalPipeline
from tests.fake_mt5 import FakeMT5
from tests.scenarios import BULLISH_ASK, BULLISH_BID, htf_bullish, ltf_bullish, to_rates

UTC = dt.timezone.utc
MAGIC = 20261001


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


# Wednesday 2026-10-07. US CPI at 08:30 New York = 12:30 UTC.
FF_WEEK = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-10-07T08:30:00-04:00", "impact": "High",
     "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-10-07T10:30:00-04:00", "impact": "Medium"},
    {"title": "ECB President Speaks", "country": "EUR", "date": "2026-10-07T09:00:00-04:00", "impact": "High"},
    {"title": "FOMC Meeting Minutes", "country": "USD", "date": "2026-10-08T14:00:00-04:00", "impact": "High"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-10-07T00:00:00-04:00", "impact": "Holiday"},
    {"title": "Broken row", "country": "USD", "date": "Tentative", "impact": "High"},
]


class StubSource:
    def __init__(self, payload=FF_WEEK, fail=False):
        self.payload, self.fail, self.calls = payload, fail, 0

    def fetch(self):
        self.calls += 1
        if self.fail:
            raise OSError("HTTP Error 429: Too Many Requests")
        return parse_ff_json(self.payload)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


# --------------------------------------------------------------------------- #
# News: parsing, sources, cache
# --------------------------------------------------------------------------- #
def test_parse_ff_json_converts_to_utc_and_skips_bad_rows():
    events = parse_ff_json(FF_WEEK)
    assert len(events) == 5  # "Tentative" row dropped
    cpi = next(e for e in events if e.title == "CPI m/m")
    assert cpi.time == utc(2026, 10, 7, 12, 30)
    assert (cpi.currency, cpi.impact, cpi.forecast) == ("USD", "High", "0.3%")
    assert events == sorted(events, key=lambda e: e.time)


def test_forexfactory_feed_fetches_json(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(FF_WEEK).encode()

    def fake_urlopen(req, timeout):
        seen["url"], seen["ua"], seen["timeout"] = req.full_url, req.get_header("User-agent"), timeout
        return Resp()

    monkeypatch.setattr("risk.news.urllib.request.urlopen", fake_urlopen)
    events = ForexFactoryFeed("https://nfs.faireconomy.media/ff_calendar_thisweek.json").fetch()
    assert len(events) == 5
    assert seen["url"].endswith("ff_calendar_thisweek.json")
    assert "aurum" in seen["ua"]


def test_file_calendar(tmp_path):
    f = tmp_path / "week.json"
    f.write_text(json.dumps(FF_WEEK))
    assert len(FileCalendar(f).fetch()) == 5


def test_calendar_refreshes_only_when_stale():
    clock, src = Clock(utc(2026, 10, 7, 6)), StubSource()
    cal = NewsCalendar(src, refresh=dt.timedelta(hours=6), clock=clock)
    cal.events(); cal.events()
    assert src.calls == 1
    clock.now += dt.timedelta(hours=7)
    cal.events()
    assert src.calls == 2


def test_calendar_uses_recent_data_when_refresh_fails():
    clock, src = Clock(utc(2026, 10, 7, 6)), StubSource()
    cal = NewsCalendar(src, refresh=dt.timedelta(hours=6), max_stale=dt.timedelta(hours=24), clock=clock)
    cal.events()
    src.fail = True
    clock.now += dt.timedelta(hours=10)
    assert len(cal.events()) == 5  # stale but usable
    assert "429" in cal.last_error
    clock.now += dt.timedelta(hours=20)
    with pytest.raises(CalendarUnavailable, match="429"):
        cal.events()


def test_calendar_disk_cache_survives_restart(tmp_path):
    clock = Clock(utc(2026, 10, 7, 6))
    NewsCalendar(StubSource(), cache_path=tmp_path / "c.json", clock=clock).events()
    clock.now += dt.timedelta(hours=8)  # past refresh, within max_stale
    offline = NewsCalendar(StubSource(fail=True), cache_path=tmp_path / "c.json", clock=clock)
    assert len(offline.events()) == 5


# --------------------------------------------------------------------------- #
# News guard
# --------------------------------------------------------------------------- #
def news_guard(fail=False, fail_closed=True):
    clock = Clock(None)
    guard = NewsGuard(NewsCalendar(StubSource(fail=fail), clock=lambda: utc(2026, 10, 7, 6)),
                      before=dt.timedelta(minutes=30), after=dt.timedelta(minutes=30), fail_closed=fail_closed)
    return guard, clock


@pytest.mark.parametrize(("now", "blocked"), [
    (utc(2026, 10, 7, 11, 59), False),  # 31 min before CPI
    (utc(2026, 10, 7, 12, 0), True),    # 30 min before
    (utc(2026, 10, 7, 12, 30), True),   # release
    (utc(2026, 10, 7, 13, 0), True),    # 30 min after
    (utc(2026, 10, 7, 13, 1), False),
    (utc(2026, 10, 7, 14, 30), False),  # medium-impact USD and high-impact EUR are ignored
])
def test_news_blackout_window(now, blocked):
    guard, _ = news_guard()
    d = guard.check(now)
    assert d.blocked is blocked
    if blocked:
        assert "CPI m/m" in d.reason and d.event.title == "CPI m/m"


def test_news_reports_next_event():
    guard, _ = news_guard()
    d = guard.check(utc(2026, 10, 7, 14))
    assert d.next_event.title == "FOMC Meeting Minutes"
    assert d.next_event.time == utc(2026, 10, 8, 18)


def test_news_fails_closed_without_calendar():
    guard, _ = news_guard(fail=True)
    d = guard.check(utc(2026, 10, 7, 9))
    assert d.blocked and "News risk unknown" in d.reason


def test_news_can_fail_open():
    guard, _ = news_guard(fail=True, fail_closed=False)
    assert not guard.check(utc(2026, 10, 7, 9)).blocked


# --------------------------------------------------------------------------- #
# Kill zones (default: Africa/Nairobi = UTC+3)
# --------------------------------------------------------------------------- #
def eat_filter():
    return KillzoneFilter.from_settings((("London", "10:00", "12:00"), ("NY AM", "16:30", "19:00")), "Africa/Nairobi")


@pytest.mark.parametrize(("now", "active"), [
    (utc(2026, 10, 7, 6, 59), None),
    (utc(2026, 10, 7, 7, 0), "London"),   # 10:00 EAT, start inclusive
    (utc(2026, 10, 7, 8, 59), "London"),
    (utc(2026, 10, 7, 9, 0), None),       # 12:00 EAT, end exclusive
    (utc(2026, 10, 7, 13, 30), "NY AM"),  # 16:30 EAT
    (utc(2026, 10, 7, 16, 0), None),      # 19:00 EAT
])
def test_killzone_windows(now, active):
    st = eat_filter().status(now)
    assert (st.active.name if st.active else None) == active


def test_killzone_end_and_next():
    f = eat_filter()
    st = f.status(utc(2026, 10, 7, 7, 30))
    assert st.ends_at == utc(2026, 10, 7, 9, 0)
    st = f.status(utc(2026, 10, 7, 10, 0))
    assert (st.next_zone.name, st.next_start) == ("NY AM", utc(2026, 10, 7, 13, 30))
    assert "next is NY AM" in f.describe(st)


def test_killzone_next_skips_weekend():
    st = eat_filter().status(utc(2026, 10, 9, 17, 0))  # Friday after NY AM
    assert (st.next_zone.name, st.next_start) == ("London", utc(2026, 10, 12, 7, 0))  # Monday


def test_killzone_follows_dst_in_new_york():
    f = KillzoneFilter([Killzone.parse("NY AM", "07:00", "10:00")], "America/New_York")
    assert f.status(utc(2026, 10, 7, 11, 0)).inside      # EDT: 07:00 NY = 11:00 UTC
    assert not f.status(utc(2026, 11, 4, 11, 0)).inside  # EST after Nov 1: 06:00 NY
    assert f.status(utc(2026, 11, 4, 12, 0)).inside


def test_killzone_rejects_inverted_window():
    with pytest.raises(ValueError, match="before end"):
        Killzone.parse("Bad", "12:00", "10:00")


# --------------------------------------------------------------------------- #
# Daily loss guard
# --------------------------------------------------------------------------- #
def acct(balance=10_000.0, equity=10_000.0, realized=0.0, open_risk=0.0, unprotected=()):
    return AccountRisk(balance, equity, realized, open_risk, tuple(unprotected))


def test_day_start_balance_includes_realized_and_floating():
    a = acct(balance=9_700, equity=9_650, realized=-300)
    assert a.day_start_balance == 10_000
    assert a.day_loss == 350


def test_daily_guard_allows_within_limit():
    d = DailyLossGuard(4.0).check(acct(equity=9_800), new_trade_risk=100)
    assert not d.blocked
    assert d.limit_amount == 400 and d.worst_case_loss == 300
    assert d.details["headroom"] == 200


def test_daily_guard_halts_at_limit():
    d = DailyLossGuard(4.0).check(acct(balance=9_600, equity=9_600, realized=-400))
    assert d.blocked and "limit reached" in d.reason
    assert d.used_percent == pytest.approx(4.0)


def test_daily_guard_counts_open_risk_and_new_trade():
    guard = DailyLossGuard(4.0)
    assert not guard.check(acct(equity=9_800, open_risk=100), new_trade_risk=100).blocked  # 400 = limit
    d = guard.check(acct(equity=9_800, open_risk=100), new_trade_risk=101)
    assert d.blocked and "if all stops hit" in d.reason and d.worst_case_loss == 401


def test_daily_guard_blocks_unprotected_positions():
    d = DailyLossGuard(4.0).check(acct(unprotected=[555]))
    assert d.blocked and "555" in d.reason


def test_daily_guard_total_floor():
    guard = DailyLossGuard(4.0, max_total_loss_percent=9.0, initial_balance=10_000)
    # account already down overall; today is fine but worst case breaches the 9,100 floor
    a = AccountRisk(9_250, 9_250, 0.0, 0.0)
    assert not guard.check(a, new_trade_risk=100).blocked   # 9,150 >= 9,100
    d = guard.check(a, new_trade_risk=200)                   # 9,050 < 9,100
    assert d.blocked and "max-loss floor 9,100.00" in d.reason


def test_daily_guard_validation():
    with pytest.raises(ValueError):
        DailyLossGuard(0)
    with pytest.raises(ValueError, match="initial_balance"):
        DailyLossGuard(4, max_total_loss_percent=8)


# --------------------------------------------------------------------------- #
# Risk manager on the simulated terminal
# --------------------------------------------------------------------------- #
LONDON_NOW = utc(2026, 10, 7, 7, 30)  # 10:30 EAT, inside London, server (UTC+3) 10:30


def server_time(bars, hours=3):
    bars = bars.copy()
    bars["time"] = bars["time"] + dt.timedelta(hours=hours)
    return bars


@pytest.fixture
def fake():
    f = FakeMT5()
    # bars stamped in broker server time (UTC+3), as a FundedNext-style server reports them
    f.rates = {FakeMT5.TIMEFRAME_H4: to_rates(server_time(htf_bullish())),
               FakeMT5.TIMEFRAME_M15: to_rates(server_time(ltf_bullish()))}
    f.tick = SimpleNamespace(time=int(utc(2026, 10, 7, 10, 30).timestamp()), bid=BULLISH_BID, ask=BULLISH_ASK, last=0.0)
    return f


def connector_for(fake):
    c = MT5Connector(trading=TradingSettings(magic_number=MAGIC), mt5_module=fake, retry_delay=0)
    c.connect()
    return c


def manager(fake, now=LONDON_NOW, source=None, **risk):
    settings = RiskSettings(**risk)
    return RiskManager.from_settings(settings, connector_for(fake), clock=Clock(now),
                                     calendar_source=source or StubSource())


def deal(server_time, profit, type_=1, commission=0.0):
    return SimpleNamespace(time=int(server_time.timestamp()), type=type_, profit=profit,
                           commission=commission, swap=0.0, fee=0.0)


def test_account_risk_from_terminal(fake):
    fake.positions = [
        SimpleNamespace(ticket=1, symbol="XAUUSD", type=0, volume=0.10, price_current=2654.0, sl=2636.5, magic=MAGIC),
        SimpleNamespace(ticket=2, symbol="XAUUSD", type=1, volume=0.20, price_current=2654.0, sl=2650.0, magic=7),  # SL in profit
        SimpleNamespace(ticket=3, symbol="XAUUSD", type=0, volume=0.05, price_current=2654.0, sl=0.0, magic=7),     # no SL
    ]
    fake.orders = [SimpleNamespace(ticket=4, symbol="XAUUSD", type=3, volume_current=0.2, price_open=2660.0, sl=2668.0, magic=MAGIC)]
    fake.deals = [
        deal(utc(2026, 10, 7, 9, 0), -150.0, commission=-3.5),  # today (server time)
        deal(utc(2026, 10, 7, 0, 5), 50.0),                      # today, just after server midnight
        deal(utc(2026, 10, 6, 23, 0), -999.0),                   # yesterday: ignored
        deal(utc(2026, 10, 7, 8, 0), 5_000.0, type_=2),          # deposit: ignored
    ]
    a = manager(fake).account_risk(utc_offset_hours=3)
    assert a.realized_today == pytest.approx(-103.5)
    # position 1: 17.50 x 0.1 lot x 100 = 175; order 4: 8.00 x 0.2 x 100 = 160; position 2 risks nothing
    assert a.open_risk == pytest.approx(335.0)
    assert a.unprotected == (3,)


def test_day_start_respects_offset_and_reset_hour(fake):
    m = manager(fake, now=utc(2026, 10, 7, 20, 30))  # server 23:30
    assert m.day_start(3) == utc(2026, 10, 7, 0, 0)
    m = manager(fake, now=utc(2026, 10, 7, 21, 30))  # server 00:30 next day
    assert m.day_start(3) == utc(2026, 10, 8, 0, 0)
    m = manager(fake, now=utc(2026, 10, 7, 20, 30), day_reset_hour=1)
    assert m.day_start(2) == utc(2026, 10, 7, 1, 0)


def test_pre_scan_order_of_guards(fake):
    assert manager(fake, now=utc(2026, 10, 7, 10, 0)).pre_scan(3).guard == "killzone"
    assert manager(fake, now=utc(2026, 10, 7, 13, 45)).pre_scan(3).allowed  # NY AM, after CPI window

    cpi = [{"title": "CPI m/m", "country": "USD", "date": "2026-10-07T07:40:00+00:00", "impact": "High"}]
    d = manager(fake, source=StubSource(cpi)).pre_scan(3)
    assert d.guard == "news" and "CPI" in d.reason

    fake.account.balance = fake.account.equity = 9_500.0
    fake.deals = [deal(utc(2026, 10, 7, 9, 0), -500.0)]  # down 5% today
    d = manager(fake).pre_scan(3)
    assert d.guard == "daily_loss" and "limit reached" in d.reason


def test_pre_trade_includes_new_trade_risk(fake):
    m = manager(fake, daily_loss_limit_percent=1.0)  # 100 USD on 10k
    assert m.pre_trade(2646.5, 2636.5, 0.10, "XAUUSD", 3).allowed  # 10.00 x 0.1 x 100 = 100
    d = m.pre_trade(2646.5, 2636.5, 0.11, "XAUUSD", 3)
    assert d.guard == "daily_loss" and d.details["new_trade_risk"] == 110.0


def test_order_expiration_is_killzone_end_in_server_time(fake):
    assert manager(fake).order_expiration(3) == utc(2026, 10, 7, 12, 0)  # 09:00 UTC -> 12:00 server
    assert manager(fake, expire_orders_at_killzone_end=False).order_expiration(3) is None


def test_snapshot_for_dashboard(fake):
    snap = manager(fake, now=utc(2026, 10, 7, 12, 10)).snapshot(3)
    json.dumps(snap)
    assert snap["killzone"]["inside"] is False and snap["killzone"]["next"] == "NY AM"
    assert snap["news"]["blocked"] is True and snap["news"]["event"]["title"] == "CPI m/m"
    assert snap["daily_loss"]["limit_amount"] == 400.0 and snap["daily_loss"]["blocked"] is False


def test_disabled_guards(fake):
    m = manager(fake, now=utc(2026, 10, 7, 12, 10), killzones_enabled=False, news_enabled=False,
                daily_loss_enabled=False)
    assert m.pre_scan(3).allowed
    assert m.snapshot(3)["news"] == {"enabled": False}


# --------------------------------------------------------------------------- #
# Pipeline integration
# --------------------------------------------------------------------------- #
def pipeline(fake, risk, execute=True, tmp_path=None):
    c = risk.connector
    return SignalPipeline(c, MT5Executor(c), AurumReportGenerator(output_dir=tmp_path),
                          StrategyParameters(server_utc_offset_hours=3), execute=execute, risk_manager=risk)


def test_pipeline_blocked_outside_killzone_does_not_scan(fake, tmp_path):
    risk = manager(fake, now=utc(2026, 10, 7, 10, 0))
    result = pipeline(fake, risk, tmp_path=tmp_path).run("XAUUSD")
    assert result.status == "blocked" and result.risk_guard == "killzone"
    assert fake.rates_calls == []  # no bars fetched, no setup evaluated
    assert fake.sent == []


def test_news_blackout_cancels_bot_pending_orders(fake, tmp_path):
    fake.orders = [
        SimpleNamespace(ticket=41, symbol="XAUUSD", type=2, volume_current=0.1, price_open=2640.0, sl=2630.0, magic=MAGIC),
        SimpleNamespace(ticket=42, symbol="XAUUSD", type=2, volume_current=0.1, price_open=2640.0, sl=2630.0, magic=999),
    ]
    fake.send_results = [{"retcode": 10009}]
    cpi = [{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-07T07:45:00+00:00", "impact": "High"}]
    result = pipeline(fake, manager(fake, source=StubSource(cpi)), tmp_path=tmp_path).run("XAUUSD")
    assert result.status == "blocked" and result.risk_guard == "news"
    assert fake.sent == [{"action": FakeMT5.TRADE_ACTION_REMOVE, "order": 41}]  # only the bot's order


def test_pipeline_trade_blocked_by_daily_loss_after_setup(fake, tmp_path):
    # 0.10 lot at 1% risk = 100 USD; allow only 0.5% (50 USD) today
    risk = manager(fake, daily_loss_limit_percent=0.5)
    result = pipeline(fake, risk, tmp_path=tmp_path).run("XAUUSD")
    assert result.status == "blocked" and result.risk_guard == "daily_loss"
    assert result.signal is not None and result.lot_size == 0.10
    assert fake.sent == []
    assert list(tmp_path.glob("*.html")) == []  # no report for a trade that will not be placed


def test_pipeline_order_expires_at_killzone_end(fake, tmp_path):
    fake.send_results = [{"retcode": 10008, "order": 7}]
    result = pipeline(fake, manager(fake), tmp_path=tmp_path).run("XAUUSD")
    assert result.status == "executed"
    [req] = fake.sent
    assert req["type_time"] == FakeMT5.ORDER_TIME_SPECIFIED
    assert req["expiration"] == int(utc(2026, 10, 7, 12, 0).timestamp())  # London end, server time


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_risk_settings_from_env(monkeypatch):
    for k, v in {
        "AURUM_KILLZONES": "London=09:00-11:00, NY AM=15:30-18:00",
        "AURUM_KILLZONE_TIMEZONE": "Etc/GMT-2",
        "AURUM_NEWS_CURRENCIES": "USD,EUR",
        "AURUM_NEWS_BEFORE_MINUTES": "0",
        "AURUM_DAILY_LOSS_LIMIT_PERCENT": "2.5",
        "AURUM_MAX_TOTAL_LOSS_PERCENT": "5",
        "AURUM_INITIAL_BALANCE": "10000",
        "AURUM_NEWS_GUARD": "false",
    }.items():
        monkeypatch.setenv(k, v)
    r = load_settings(env_file=None).risk
    assert r.killzones == (("London", "09:00", "11:00"), ("NY AM", "15:30", "18:00"))
    assert r.killzone_timezone == "Etc/GMT-2"
    assert r.news_currencies == ("USD", "EUR") and r.news_before_minutes == 0
    assert (r.daily_loss_limit_percent, r.max_total_loss_percent, r.initial_balance) == (2.5, 5.0, 10_000.0)
    assert r.news_enabled is False


def test_risk_settings_defaults_are_protective():
    r = load_settings(env_file=None).risk
    assert r.news_enabled and r.news_fail_closed and r.killzones_enabled and r.daily_loss_enabled
    assert r.daily_loss_limit_percent == 4.0


def test_invalid_killzone_env(monkeypatch):
    monkeypatch.setenv("AURUM_KILLZONES", "London 10-12")
    with pytest.raises(ValueError, match="Name=HH:MM-HH:MM"):
        load_settings(env_file=None)


def test_total_loss_needs_initial_balance():
    with pytest.raises(ValueError, match="initial_balance"):
        RiskSettings(max_total_loss_percent=8)
