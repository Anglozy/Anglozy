"""MetaTrader 5 terminal connection and market data.

This package is the only place in the project that talks to the ``MetaTrader5``
library. Everything it returns is plain Python data (dataclasses or pandas
DataFrames) so the rest of the code never handles raw MT5 objects.

Example:

    from config.settings import load_settings
    from mt5.connector import MT5Connector

    settings = load_settings()
    with MT5Connector(settings.credentials, settings.trading) as conn:
        print(conn.get_tick("XAUUSD"))
        print(conn.get_bars("XAUUSD", "M15", count=200).tail())
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any, Literal

import pandas as pd

from config.settings import MT5Credentials, TradingSettings

try:
    import MetaTrader5 as _mt5
except ImportError:  # not installed (e.g. Linux CI); tests inject a fake module
    _mt5 = None

logger = logging.getLogger(__name__)

# ACCOUNT_TRADE_MODE_* values from the MT5 API.
ACCOUNT_TRADE_MODES = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
ACCOUNT_TRADE_MODE_REAL = 2

TIMEFRAMES = (
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1",
)


class MT5Error(Exception):
    """Base class for MT5 errors."""


class MT5ConnectionError(MT5Error):
    """The terminal could not be initialised, logged in or verified."""


class MT5DataError(MT5Error):
    """Market data or symbol information could not be retrieved."""


# --------------------------------------------------------------------------- #
# Plain data returned to callers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AccountSnapshot:
    login: int
    server: str
    name: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int
    trade_mode: str  # DEMO / CONTEST / REAL
    trade_allowed: bool

    @property
    def is_real(self) -> bool:
        return self.trade_mode == "REAL"


@dataclass(frozen=True)
class Tick:
    symbol: str
    time: datetime
    bid: float
    ask: float
    last: float
    spread_points: float


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float  # account-currency value of one tick move for 1 lot (loss side)
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int  # minimum SL/TP distance from price, in points
    freeze_level: int
    filling_mode: int  # bitmask: 1 = FOK allowed, 2 = IOC allowed
    trade_mode: int  # SYMBOL_TRADE_MODE_*: 0 disabled, 1 long only, 2 short only, 3 close only, 4 full


@dataclass(frozen=True)
class Exposure:
    """An open position or pending order anywhere on the account (any magic number)."""

    ticket: int
    symbol: str
    kind: Literal["position", "order"]
    side: Literal["BUY", "SELL"]
    volume: float
    price: float  # current price for positions, order price for pending orders
    stop_loss: float  # 0.0 = none
    magic: int


# ORDER_TYPE_* values for pending orders that buy: BUY_LIMIT, BUY_STOP, BUY_STOP_LIMIT
_BUY_ORDER_TYPES = {0, 2, 4, 6}
# DEAL_TYPE_BUY / DEAL_TYPE_SELL: trading deals (excludes balance, credit, bonus operations)
_TRADE_DEAL_TYPES = {0, 1}


# --------------------------------------------------------------------------- #
# Connector
# --------------------------------------------------------------------------- #
class MT5Connector:
    """Owns the MT5 terminal session: initialise, log in, verify, fetch data."""

    def __init__(
        self,
        credentials: MT5Credentials | None = None,
        trading: TradingSettings | None = None,
        mt5_module: ModuleType | Any | None = None,
        connect_attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        self.credentials = credentials or MT5Credentials()
        self.trading = trading or TradingSettings()
        self.mt5 = mt5_module if mt5_module is not None else _mt5
        self.connect_attempts = max(1, connect_attempts)
        self.retry_delay = retry_delay
        self.account: AccountSnapshot | None = None
        self._connected = False

    # ------------------------------------------------------------ lifecycle
    def connect(self) -> AccountSnapshot:
        """Initialise the terminal, log in and verify the account can trade.

        Raises :class:`MT5ConnectionError` on failure. Refuses to stay connected
        to a real-money account unless ``trading.allow_live_trading`` is set.
        """
        if self.mt5 is None:
            raise MT5ConnectionError(
                "The MetaTrader5 package is not installed. It requires Windows and "
                "a local MT5 terminal: pip install MetaTrader5"
            )

        last_error = None
        for attempt in range(1, self.connect_attempts + 1):
            if self._initialize():
                break
            last_error = self._last_error()
            logger.warning(
                "MT5 initialize failed (attempt %d/%d): %s",
                attempt, self.connect_attempts, last_error,
            )
            self.mt5.shutdown()
            if attempt < self.connect_attempts:
                time.sleep(self.retry_delay)
        else:
            raise MT5ConnectionError(f"Could not initialise MT5 terminal: {last_error}")

        self._connected = True
        try:
            self.account = self.verify_connection()
        except MT5ConnectionError:
            self.disconnect()
            raise
        return self.account

    def disconnect(self) -> None:
        if self.mt5 is not None and self._connected:
            self.mt5.shutdown()
            logger.info("MT5 connection closed")
        self._connected = False

    def __enter__(self) -> MT5Connector:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    @property
    def is_connected(self) -> bool:
        if not self._connected:
            return False
        info = self.mt5.terminal_info()
        return bool(info and info.connected)

    def verify_connection(self) -> AccountSnapshot:
        """Check the terminal is online and the account is usable; return a snapshot."""
        terminal = self.mt5.terminal_info()
        if terminal is None:
            raise MT5ConnectionError(f"terminal_info() failed: {self._last_error()}")
        if not terminal.connected:
            raise MT5ConnectionError("MT5 terminal is not connected to the trade server")

        account = self.account_info()

        if account.is_real and not self.trading.allow_live_trading:
            raise MT5ConnectionError(
                f"Account {account.login} on {account.server} is a REAL account. "
                "Set AURUM_ALLOW_LIVE_TRADING=true to trade it."
            )
        if not terminal.trade_allowed:
            logger.warning("Algo Trading is disabled in the terminal; orders will be rejected")
        if not account.trade_allowed:
            logger.warning("Trading is not allowed for account %s", account.login)

        logger.info(
            "Connected to MT5: account %s (%s) on %s, balance %.2f %s, leverage 1:%d",
            account.login, account.trade_mode, account.server,
            account.balance, account.currency, account.leverage,
        )
        return account

    # ------------------------------------------------------------ account
    def account_info(self) -> AccountSnapshot:
        self._require_connection()
        info = self.mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info() failed: {self._last_error()}")
        return AccountSnapshot(
            login=info.login,
            server=info.server,
            name=info.name,
            currency=info.currency,
            balance=info.balance,
            equity=info.equity,
            margin_free=info.margin_free,
            leverage=info.leverage,
            trade_mode=ACCOUNT_TRADE_MODES.get(info.trade_mode, str(info.trade_mode)),
            trade_allowed=bool(info.trade_allowed),
        )

    def exposures(self) -> list[Exposure]:
        """Every open position and pending order on the account, for risk checks."""
        self._require_connection()
        positions = self.mt5.positions_get()
        orders = self.mt5.orders_get()
        if positions is None or orders is None:
            raise MT5DataError(f"Could not read positions/orders: {self._last_error()}")
        out = [
            Exposure(p.ticket, p.symbol, "position", "BUY" if p.type == 0 else "SELL",
                     p.volume, p.price_current, p.sl or 0.0, p.magic)
            for p in positions
        ]
        out += [
            Exposure(o.ticket, o.symbol, "order", "BUY" if o.type in _BUY_ORDER_TYPES else "SELL",
                     o.volume_current, o.price_open, o.sl or 0.0, o.magic)
            for o in orders
        ]
        return out

    def realized_pnl_since(self, since: datetime) -> float:
        """Closed-trade P&L (profit + commission + swap + fee) of deals from ``since``.

        ``since`` is broker server wall time expressed as a UTC-aware datetime,
        the same convention MT5 uses for bar and tick times.
        """
        self._require_connection()
        deals = self.mt5.history_deals_get(since, since + timedelta(days=7))
        if deals is None:
            raise MT5DataError(f"Could not read deal history: {self._last_error()}")
        start = since.timestamp()
        return float(sum(
            d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
            for d in deals
            if d.type in _TRADE_DEAL_TYPES and d.time >= start
        ))

    # ------------------------------------------------------------ market data
    def ensure_symbol(self, symbol: str | None = None) -> str:
        """Make sure ``symbol`` exists and is in Market Watch. Returns the symbol."""
        self._require_connection()
        symbol = symbol or self.trading.symbol
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise MT5DataError(f"Unknown symbol {symbol!r}. Check the broker's name for it (e.g. XAUUSD.m)")
        if not info.visible and not self.mt5.symbol_select(symbol, True):
            raise MT5DataError(f"Could not add {symbol} to Market Watch: {self._last_error()}")
        return symbol

    def symbol_spec(self, symbol: str | None = None) -> SymbolSpec:
        symbol = self.ensure_symbol(symbol)
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise MT5DataError(f"symbol_info({symbol}) failed: {self._last_error()}")
        # Prefer the loss-side tick value: it is what a stop-out actually costs.
        tick_value = getattr(info, "trade_tick_value_loss", 0) or info.trade_tick_value
        return SymbolSpec(
            name=info.name,
            digits=info.digits,
            point=info.point,
            tick_size=info.trade_tick_size,
            tick_value=tick_value,
            contract_size=info.trade_contract_size,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            stops_level=info.trade_stops_level,
            freeze_level=info.trade_freeze_level,
            filling_mode=info.filling_mode,
            trade_mode=info.trade_mode,
        )

    def get_tick(self, symbol: str | None = None) -> Tick:
        """Latest bid/ask tick for ``symbol`` (default: the configured symbol)."""
        symbol = self.ensure_symbol(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5DataError(f"No tick for {symbol}: {self._last_error()}")
        if tick.bid <= 0 or tick.ask <= 0:
            raise MT5DataError(f"No valid prices for {symbol} (market may be closed)")
        point = self.mt5.symbol_info(symbol).point
        return Tick(
            symbol=symbol,
            time=datetime.fromtimestamp(tick.time, tz=timezone.utc),
            bid=tick.bid,
            ask=tick.ask,
            last=tick.last,
            spread_points=round((tick.ask - tick.bid) / point) if point else 0,
        )

    def get_bars(
        self,
        symbol: str | None = None,
        timeframe: str = "M15",
        count: int = 500,
        include_current: bool = False,
    ) -> pd.DataFrame:
        """Most recent ``count`` OHLC bars, oldest first.

        By default the still-forming bar is excluded so signal detection only sees
        closed candles. Columns: time (UTC), open, high, low, close, tick_volume,
        spread, real_volume.
        """
        if count <= 0:
            raise ValueError("count must be positive")
        symbol = self.ensure_symbol(symbol)
        mt5_timeframe = self._timeframe(timeframe)
        start = 0 if include_current else 1
        rates = self.mt5.copy_rates_from_pos(symbol, mt5_timeframe, start, count)
        if rates is None or len(rates) == 0:
            raise MT5DataError(f"No {timeframe} bars for {symbol}: {self._last_error()}")

        bars = pd.DataFrame(rates)
        bars["time"] = pd.to_datetime(bars["time"], unit="s", utc=True)
        return bars.reset_index(drop=True)

    # ------------------------------------------------------------ internals
    def _initialize(self) -> bool:
        c = self.credentials
        kwargs: dict[str, Any] = {"timeout": c.timeout_ms}
        if c.path:
            kwargs["path"] = c.path
        if c.has_login:
            kwargs.update(login=c.login, password=c.password, server=c.server)
            logger.info("Initialising MT5 and logging in to %s on %s", c.login, c.server)
        else:
            logger.info("Initialising MT5 with the terminal's current account")
        return bool(self.mt5.initialize(**kwargs))

    def _timeframe(self, timeframe: str) -> int:
        name = timeframe.upper()
        if name not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {', '.join(TIMEFRAMES)}")
        return getattr(self.mt5, f"TIMEFRAME_{name}")

    def _require_connection(self) -> None:
        if not self._connected:
            raise MT5ConnectionError("Not connected. Call connect() first.")

    def _last_error(self) -> str:
        try:
            code, description = self.mt5.last_error()
            return f"[{code}] {description}"
        except Exception:  # pragma: no cover - defensive
            return "unknown error"
