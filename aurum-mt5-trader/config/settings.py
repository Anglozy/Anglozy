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
DEFAULT_MAGIC_NUMBER = 20260930
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
class Settings:
    credentials: MT5Credentials
    trading: TradingSettings


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
    return Settings(credentials=credentials, trading=trading)


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


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
