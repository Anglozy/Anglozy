"""Terminal credentials and trading settings.

Values are resolved in this order (first wins):
    1. Environment variables (optionally loaded from a git-ignored ``.env`` file)
    2. The defaults in this file

Never put a password or account number in this file. Set ``MT5_LOGIN``,
``MT5_PASSWORD`` and ``MT5_SERVER`` in the environment or in ``.env`` instead
(see ``.env.example``). If no login is configured, the connector attaches to
whatever account the running MT5 terminal is already logged into.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ----------------------------------------------------------------- defaults
DEFAULT_SYMBOL = "XAUUSD"
DEFAULT_MAGIC_NUMBER = 20261001
DEFAULT_RISK_PERCENT = 1.0
DEFAULT_DEVIATION_POINTS = 20
DEFAULT_TIMEOUT_MS = 60_000


@dataclass(frozen=True)
class MT5Credentials:
    login: int | None = None
    password: str | None = field(default=None, repr=False)
    server: str | None = None
    path: str | None = None  # path to terminal64.exe; None lets MT5 find it
    timeout_ms: int = DEFAULT_TIMEOUT_MS

    @property
    def has_login(self) -> bool:
        return self.login is not None


@dataclass(frozen=True)
class TradingSettings:
    symbol: str = DEFAULT_SYMBOL
    magic_number: int = DEFAULT_MAGIC_NUMBER
    risk_percent: float = DEFAULT_RISK_PERCENT
    deviation_points: int = DEFAULT_DEVIATION_POINTS
    allow_live_trading: bool = False  # demo accounts only unless explicitly enabled

    def __post_init__(self) -> None:
        if not 0 < self.risk_percent <= 10:
            raise ValueError(f"risk_percent must be in (0, 10], got {self.risk_percent}")
        if self.deviation_points < 0:
            raise ValueError("deviation_points must be >= 0")


@dataclass(frozen=True)
class ChartSettings:
    """TradingView screenshots embedded in Aurum reports."""

    enabled: bool = True
    exchange: str = "OANDA"  # TradingView prefix, e.g. OANDA:XAUUSD
    chromium_path: str | None = None  # None = Playwright's own Chromium


FOREXFACTORY_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
DEFAULT_KILLZONES = (("London", "10:00", "12:00"), ("NY AM", "16:30", "19:00"))


@dataclass(frozen=True)
class RiskSettings:
    """Pre-trade guards. Defaults are deliberately conservative for a funded account."""

    # News guard: no new trades from `before` minutes ahead of a high-impact event
    # until `after` minutes past it.
    news_enabled: bool = True
    news_currencies: tuple[str, ...] = ("USD",)
    news_impacts: tuple[str, ...] = ("High",)
    news_before_minutes: int = 30
    news_after_minutes: int = 30
    news_fail_closed: bool = True  # calendar unavailable -> block new trades
    news_cancel_pending: bool = True  # cancel the bot's pending orders during a blackout
    news_calendar_url: str = FOREXFACTORY_WEEK_URL
    news_calendar_file: str | None = None  # local JSON in the same format, overrides the URL

    # Kill zones: setups are only evaluated inside these windows (local times in killzone_timezone).
    killzones_enabled: bool = True
    killzone_timezone: str = "Africa/Nairobi"  # fixed UTC+3, no daylight saving
    killzones: tuple[tuple[str, str, str], ...] = DEFAULT_KILLZONES
    expire_orders_at_killzone_end: bool = True

    # Daily loss guard (FundedNext-style). The bot stops opening trades well before the firm's limit.
    daily_loss_enabled: bool = True
    daily_loss_limit_percent: float = 4.0  # of start-of-day balance; firm limit is typically 5%
    max_total_loss_percent: float | None = None  # of initial_balance, e.g. 9 for a 10% firm limit
    initial_balance: float | None = None
    day_reset_hour: int = 0  # broker server hour at which the trading day starts

    def __post_init__(self) -> None:
        if not 0 < self.daily_loss_limit_percent <= 50:
            raise ValueError("daily_loss_limit_percent must be in (0, 50]")
        if self.max_total_loss_percent is not None and not 0 < self.max_total_loss_percent <= 100:
            raise ValueError("max_total_loss_percent must be in (0, 100]")
        if self.max_total_loss_percent is not None and not self.initial_balance:
            raise ValueError("initial_balance is required when max_total_loss_percent is set")
        if self.news_before_minutes < 0 or self.news_after_minutes < 0:
            raise ValueError("news window minutes must be >= 0")
        if not 0 <= self.day_reset_hour <= 23:
            raise ValueError("day_reset_hour must be 0-23")


@dataclass(frozen=True)
class Settings:
    credentials: MT5Credentials
    trading: TradingSettings
    charts: ChartSettings = field(default_factory=ChartSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)


def load_settings(env_file: str | Path | None = PROJECT_ROOT / ".env") -> Settings:
    """Build :class:`Settings` from environment variables and defaults."""
    if load_dotenv is not None and env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)

    credentials = MT5Credentials(
        login=_env_int("MT5_LOGIN"),
        password=os.getenv("MT5_PASSWORD") or None,
        server=os.getenv("MT5_SERVER") or None,
        path=os.getenv("MT5_PATH") or None,
        timeout_ms=_env_int("MT5_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS,
    )
    if credentials.has_login and not (credentials.password and credentials.server):
        raise ValueError("MT5_LOGIN is set, so MT5_PASSWORD and MT5_SERVER are required too")

    trading = TradingSettings(
        symbol=os.getenv("AURUM_SYMBOL", DEFAULT_SYMBOL),
        magic_number=_env_int("AURUM_MAGIC_NUMBER") or DEFAULT_MAGIC_NUMBER,
        risk_percent=_env_float("AURUM_RISK_PERCENT", DEFAULT_RISK_PERCENT),
        deviation_points=_env_int("AURUM_DEVIATION_POINTS") or DEFAULT_DEVIATION_POINTS,
        allow_live_trading=_env_bool("AURUM_ALLOW_LIVE_TRADING"),
    )
    charts = ChartSettings(
        enabled=_env_bool("AURUM_CHARTS_ENABLED", default=True),
        exchange=os.getenv("AURUM_TV_EXCHANGE") or "OANDA",
        chromium_path=os.getenv("AURUM_CHROMIUM_PATH") or None,
    )
    risk = RiskSettings(
        news_enabled=_env_bool("AURUM_NEWS_GUARD", default=True),
        news_currencies=_env_list("AURUM_NEWS_CURRENCIES") or ("USD",),
        news_impacts=_env_list("AURUM_NEWS_IMPACTS") or ("High",),
        news_before_minutes=_env_int("AURUM_NEWS_BEFORE_MINUTES") if _env_int("AURUM_NEWS_BEFORE_MINUTES") is not None else 30,
        news_after_minutes=_env_int("AURUM_NEWS_AFTER_MINUTES") if _env_int("AURUM_NEWS_AFTER_MINUTES") is not None else 30,
        news_fail_closed=_env_bool("AURUM_NEWS_FAIL_CLOSED", default=True),
        news_cancel_pending=_env_bool("AURUM_NEWS_CANCEL_PENDING", default=True),
        news_calendar_url=os.getenv("AURUM_NEWS_CALENDAR_URL") or FOREXFACTORY_WEEK_URL,
        news_calendar_file=os.getenv("AURUM_NEWS_CALENDAR_FILE") or None,
        killzones_enabled=_env_bool("AURUM_KILLZONES_ENABLED", default=True),
        killzone_timezone=os.getenv("AURUM_KILLZONE_TIMEZONE") or "Africa/Nairobi",
        killzones=_env_killzones("AURUM_KILLZONES") or DEFAULT_KILLZONES,
        expire_orders_at_killzone_end=_env_bool("AURUM_EXPIRE_AT_KILLZONE_END", default=True),
        daily_loss_enabled=_env_bool("AURUM_DAILY_LOSS_GUARD", default=True),
        daily_loss_limit_percent=_env_float("AURUM_DAILY_LOSS_LIMIT_PERCENT", 4.0),
        max_total_loss_percent=_env_float("AURUM_MAX_TOTAL_LOSS_PERCENT", 0.0) or None,
        initial_balance=_env_float("AURUM_INITIAL_BALANCE", 0.0) or None,
        day_reset_hour=_env_int("AURUM_DAY_RESET_HOUR") or 0,
    )
    return Settings(credentials=credentials, trading=trading, charts=charts, risk=risk)


def _env_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {value!r}") from None


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {value!r}") from None


def _env_list(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


def _env_killzones(name: str) -> tuple[tuple[str, str, str], ...]:
    """``London=10:00-12:00,NY AM=16:30-19:00`` -> (("London", "10:00", "12:00"), ...)."""
    zones = []
    for item in _env_list(name):
        try:
            label, window = item.split("=", 1)
            start, end = window.split("-", 1)
        except ValueError:
            raise ValueError(f"{name}: expected 'Name=HH:MM-HH:MM', got {item!r}") from None
        zones.append((label.strip(), start.strip(), end.strip()))
    return tuple(zones)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}
