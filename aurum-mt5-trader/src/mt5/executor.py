"""Risk-based position sizing and order execution for MetaTrader 5.

Example:

    from config.settings import load_settings
    from mt5.connector import MT5Connector
    from mt5.executor import MT5Executor

    settings = load_settings()
    with MT5Connector(settings.credentials, settings.trading) as conn:
        executor = MT5Executor(conn)
        result = executor.place_limit_order("BUY", price=2648.50, stop_loss=2641.20,
                                            take_profit=2663.10)
        if not result.success:
            print(result.message)

Rules enforced here (see CLAUDE.md, "Trading safety"):
    * every order has a stop loss;
    * lot size comes from risk % and stop distance unless explicitly given;
    * every ``order_send`` result is checked and logged, never assumed filled.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from config.settings import TradingSettings

from .connector import MT5Connector, SymbolSpec, Tick

logger = logging.getLogger(__name__)

Side = Literal["BUY", "SELL"]

# SYMBOL_FILLING_* flags in SymbolSpec.filling_mode.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

# SYMBOL_TRADE_MODE_* values.
SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_LONGONLY = 1
SYMBOL_TRADE_MODE_SHORTONLY = 2
SYMBOL_TRADE_MODE_CLOSEONLY = 3

MAX_COMMENT_LENGTH = 31  # MT5 truncates/rejects longer comments


# --------------------------------------------------------------------------- #
# Trade server return codes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Retcode:
    name: str
    description: str
    outcome: Literal["success", "retry", "fatal"]


TRADE_RETCODES: dict[int, Retcode] = {
    10004: Retcode("TRADE_RETCODE_REQUOTE", "Requote", "retry"),
    10006: Retcode("TRADE_RETCODE_REJECT", "Request rejected", "fatal"),
    10007: Retcode("TRADE_RETCODE_CANCEL", "Request canceled by trader", "fatal"),
    10008: Retcode("TRADE_RETCODE_PLACED", "Order placed", "success"),
    10009: Retcode("TRADE_RETCODE_DONE", "Request completed", "success"),
    10010: Retcode("TRADE_RETCODE_DONE_PARTIAL", "Only part of the request was completed", "success"),
    10011: Retcode("TRADE_RETCODE_ERROR", "Request processing error", "fatal"),
    10012: Retcode("TRADE_RETCODE_TIMEOUT", "Request canceled by timeout", "retry"),
    10013: Retcode("TRADE_RETCODE_INVALID", "Invalid request", "fatal"),
    10014: Retcode("TRADE_RETCODE_INVALID_VOLUME", "Invalid volume in the request", "fatal"),
    10015: Retcode("TRADE_RETCODE_INVALID_PRICE", "Invalid price in the request", "fatal"),
    10016: Retcode("TRADE_RETCODE_INVALID_STOPS", "Invalid stops in the request", "fatal"),
    10017: Retcode("TRADE_RETCODE_TRADE_DISABLED", "Trade is disabled", "fatal"),
    10018: Retcode("TRADE_RETCODE_MARKET_CLOSED", "Market is closed", "fatal"),
    10019: Retcode("TRADE_RETCODE_NO_MONEY", "Not enough money (insufficient margin)", "fatal"),
    10020: Retcode("TRADE_RETCODE_PRICE_CHANGED", "Prices changed", "retry"),
    10021: Retcode("TRADE_RETCODE_PRICE_OFF", "No quotes to process the request", "retry"),
    10022: Retcode("TRADE_RETCODE_INVALID_EXPIRATION", "Invalid order expiration date", "fatal"),
    10023: Retcode("TRADE_RETCODE_ORDER_CHANGED", "Order state changed", "fatal"),
    10024: Retcode("TRADE_RETCODE_TOO_MANY_REQUESTS", "Too frequent requests", "retry"),
    10025: Retcode("TRADE_RETCODE_NO_CHANGES", "No changes in request", "fatal"),
    10026: Retcode("TRADE_RETCODE_SERVER_DISABLES_AT", "Autotrading disabled by server", "fatal"),
    10027: Retcode("TRADE_RETCODE_CLIENT_DISABLES_AT", "Autotrading disabled by client terminal (enable Algo Trading)", "fatal"),
    10028: Retcode("TRADE_RETCODE_LOCKED", "Request locked for processing", "fatal"),
    10029: Retcode("TRADE_RETCODE_FROZEN", "Order or position frozen", "fatal"),
    10030: Retcode("TRADE_RETCODE_INVALID_FILL", "Invalid order filling type", "retry"),
    10031: Retcode("TRADE_RETCODE_CONNECTION", "No connection with the trade server", "retry"),
    10032: Retcode("TRADE_RETCODE_ONLY_REAL", "Operation is allowed only for live accounts", "fatal"),
    10033: Retcode("TRADE_RETCODE_LIMIT_ORDERS", "Pending orders limit reached", "fatal"),
    10034: Retcode("TRADE_RETCODE_LIMIT_VOLUME", "Volume limit for the symbol reached", "fatal"),
    10035: Retcode("TRADE_RETCODE_INVALID_ORDER", "Incorrect or prohibited order type", "fatal"),
    10036: Retcode("TRADE_RETCODE_POSITION_CLOSED", "Position already closed", "fatal"),
    10038: Retcode("TRADE_RETCODE_INVALID_CLOSE_VOLUME", "Close volume exceeds position volume", "fatal"),
    10039: Retcode("TRADE_RETCODE_CLOSE_ORDER_EXIST", "A close order already exists", "fatal"),
    10040: Retcode("TRADE_RETCODE_LIMIT_POSITIONS", "Open positions limit reached", "fatal"),
    10041: Retcode("TRADE_RETCODE_REJECT_CANCEL", "Pending order activation rejected and canceled", "fatal"),
    10042: Retcode("TRADE_RETCODE_LONG_ONLY", "Only long positions allowed", "fatal"),
    10043: Retcode("TRADE_RETCODE_SHORT_ONLY", "Only short positions allowed", "fatal"),
    10044: Retcode("TRADE_RETCODE_CLOSE_ONLY", "Only position closing allowed", "fatal"),
    10045: Retcode("TRADE_RETCODE_FIFO_CLOSE", "Position closing allowed only by FIFO rule", "fatal"),
}

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_INVALID_FILL = 10030


def describe_retcode(retcode: int) -> Retcode:
    return TRADE_RETCODES.get(retcode, Retcode(f"RETCODE_{retcode}", "Unknown trade server return code", "fatal"))


# --------------------------------------------------------------------------- #
# Errors and results
# --------------------------------------------------------------------------- #
class OrderValidationError(ValueError):
    """The order was rejected locally before being sent to the broker."""


class InsufficientMarginError(OrderValidationError):
    """Free margin does not cover the order."""


class RiskTooSmallError(OrderValidationError):
    """Even the minimum lot would risk more than the allowed amount."""


@dataclass
class OrderResult:
    success: bool
    retcode: int | None
    retcode_name: str
    message: str
    order: int = 0  # order ticket
    deal: int = 0  # deal ticket (market orders)
    volume: float = 0.0
    price: float = 0.0
    request: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure sizing helper
# --------------------------------------------------------------------------- #
def lot_size_for_risk(
    risk_amount: float,
    sl_distance: float,
    tick_size: float,
    tick_value: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> float:
    """Largest lot size whose stop-loss hit loses at most ``risk_amount``.

    Rounded *down* to ``volume_step`` and capped at ``volume_max``. Raises
    :class:`RiskTooSmallError` if even ``volume_min`` would exceed the risk.
    """
    if sl_distance <= 0:
        raise OrderValidationError("Stop loss distance must be positive")
    if tick_size <= 0 or tick_value <= 0 or volume_step <= 0:
        raise OrderValidationError("Symbol tick size, tick value and volume step must be positive")

    loss_per_lot = sl_distance / tick_size * tick_value
    raw_lots = risk_amount / loss_per_lot
    # Epsilon guards against float error flooring e.g. 0.13 / 0.01 to 12.
    lots = math.floor(raw_lots / volume_step + 1e-9) * volume_step
    lots = min(lots, volume_max)

    if lots < volume_min:
        raise RiskTooSmallError(
            f"Risk {risk_amount:.2f} over a {sl_distance} stop allows {raw_lots:.4f} lots, "
            f"below the minimum {volume_min}. The minimum lot would risk "
            f"{volume_min * loss_per_lot:.2f}; widen risk or tighten the stop."
        )
    return round(lots, _decimals(volume_step))


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #
class MT5Executor:
    """Sizes and sends market and limit orders tagged with a magic number."""

    def __init__(
        self,
        connector: MT5Connector,
        settings: TradingSettings | None = None,
        max_retries: int = 2,
    ) -> None:
        self.connector = connector
        self.mt5 = connector.mt5
        self.settings = settings or connector.trading
        self.max_retries = max(0, max_retries)

    # ------------------------------------------------------------ sizing
    def calculate_lot_size(
        self,
        entry_price: float,
        stop_loss: float,
        symbol: str | None = None,
        risk_percent: float | None = None,
    ) -> float:
        """Lot size risking ``risk_percent`` of account balance between entry and stop."""
        risk_percent = self.settings.risk_percent if risk_percent is None else risk_percent
        if not 0 < risk_percent <= 100:
            raise OrderValidationError(f"risk_percent must be in (0, 100], got {risk_percent}")

        account = self.connector.account_info()
        spec = self.connector.symbol_spec(symbol or self.settings.symbol)
        risk_amount = account.balance * risk_percent / 100
        lots = lot_size_for_risk(
            risk_amount=risk_amount,
            sl_distance=abs(entry_price - stop_loss),
            tick_size=spec.tick_size,
            tick_value=spec.tick_value,
            volume_min=spec.volume_min,
            volume_max=spec.volume_max,
            volume_step=spec.volume_step,
        )
        logger.info(
            "Position size %s: %.2f lots risking %.2f%% (%.2f %s) over %.*f stop",
            spec.name, lots, risk_percent, risk_amount, account.currency,
            spec.digits, abs(entry_price - stop_loss),
        )
        return lots

    # ------------------------------------------------------------ orders
    def place_market_order(
        self,
        side: Side,
        stop_loss: float,
        take_profit: float | None = None,
        volume: float | None = None,
        symbol: str | None = None,
        risk_percent: float | None = None,
        comment: str = "aurum",
    ) -> OrderResult:
        """Open a position at market. Volume is risk-sized unless given."""
        side = _validate_side(side)
        symbol = symbol or self.settings.symbol
        spec = self.connector.symbol_spec(symbol)
        tick = self.connector.get_tick(symbol)
        price = tick.ask if side == "BUY" else tick.bid

        stop_loss, take_profit = self._validate_stops(spec, tick, side, price, stop_loss, take_profit)
        volume = self._resolve_volume(spec, price, stop_loss, volume, risk_percent)
        order_type = self.mt5.ORDER_TYPE_BUY if side == "BUY" else self.mt5.ORDER_TYPE_SELL
        self._check_margin(order_type, symbol, volume, price)

        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": stop_loss,
            "tp": take_profit or 0.0,
            "deviation": self.settings.deviation_points,
            "magic": self.settings.magic_number,
            "comment": comment[:MAX_COMMENT_LENGTH],
            "type_time": self.mt5.ORDER_TIME_GTC,
        }
        return self._send(request, self._market_filling_modes(spec), side=side)

    def place_limit_order(
        self,
        side: Side,
        price: float,
        stop_loss: float,
        take_profit: float | None = None,
        volume: float | None = None,
        symbol: str | None = None,
        risk_percent: float | None = None,
        expiration: datetime | None = None,
        comment: str = "aurum",
    ) -> OrderResult:
        """Place a Buy Limit (below ask) or Sell Limit (above bid) pending order."""
        side = _validate_side(side)
        symbol = symbol or self.settings.symbol
        spec = self.connector.symbol_spec(symbol)
        tick = self.connector.get_tick(symbol)
        price = _normalize_price(price, spec)

        if side == "BUY" and price >= tick.ask:
            raise OrderValidationError(f"Buy limit {price} must be below the ask {tick.ask}")
        if side == "SELL" and price <= tick.bid:
            raise OrderValidationError(f"Sell limit {price} must be above the bid {tick.bid}")
        min_distance = spec.stops_level * spec.point
        reference = tick.ask if side == "BUY" else tick.bid
        if min_distance and abs(reference - price) < min_distance:
            raise OrderValidationError(
                f"Limit price {price} is closer than the broker's stops level "
                f"({spec.stops_level} points) to the market"
            )

        stop_loss, take_profit = self._validate_stops(spec, None, side, price, stop_loss, take_profit)
        volume = self._resolve_volume(spec, price, stop_loss, volume, risk_percent)
        order_type = self.mt5.ORDER_TYPE_BUY_LIMIT if side == "BUY" else self.mt5.ORDER_TYPE_SELL_LIMIT
        self._check_margin(order_type, symbol, volume, price)

        request = {
            "action": self.mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": stop_loss,
            "tp": take_profit or 0.0,
            "magic": self.settings.magic_number,
            "comment": comment[:MAX_COMMENT_LENGTH],
            "type_time": self.mt5.ORDER_TIME_GTC,
        }
        if expiration is not None:
            request["type_time"] = self.mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = int(expiration.timestamp())
        return self._send(request, [self.mt5.ORDER_FILLING_RETURN], side=side)

    def open_positions(self, symbol: str | None = None) -> list[Any]:
        """Open positions carrying this executor's magic number."""
        positions = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        return [p for p in positions or () if p.magic == self.settings.magic_number]

    def pending_orders(self, symbol: str | None = None) -> list[Any]:
        """Pending orders carrying this executor's magic number."""
        orders = self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get()
        return [o for o in orders or () if o.magic == self.settings.magic_number]

    # ------------------------------------------------------------ internals
    def _send(self, request: dict[str, Any], filling_modes: list[int], side: Side) -> OrderResult:
        """Send ``request``, retrying transient failures, and interpret the retcode."""
        is_market = request["action"] == self.mt5.TRADE_ACTION_DEAL
        fills = list(filling_modes)
        request["type_filling"] = fills.pop(0)
        label = f"{side} {'market' if is_market else 'limit'} {request['volume']} {request['symbol']}"

        for attempt in range(1, self.max_retries + 2):
            logger.info(
                "Sending %s @ %s sl=%s tp=%s magic=%s (attempt %d)",
                label, request["price"], request["sl"], request["tp"], request["magic"], attempt,
            )
            result = self.mt5.order_send(request)

            if result is None:
                code, description = self._last_error()
                message = f"order_send returned no result: [{code}] {description}"
                logger.error("%s failed: %s", label, message)
                if attempt <= self.max_retries:
                    continue
                return OrderResult(False, None, "NO_RESULT", message, request=dict(request))

            info = describe_retcode(result.retcode)
            broker_comment = f" ({result.comment})" if getattr(result, "comment", "") else ""

            if info.outcome == "success":
                log = logger.warning if result.retcode == 10010 else logger.info
                log(
                    "%s %s: order=%s deal=%s volume=%s price=%s",
                    label, info.name, result.order, result.deal, result.volume, result.price,
                )
                return OrderResult(
                    True, result.retcode, info.name, info.description,
                    order=result.order, deal=result.deal,
                    volume=result.volume, price=result.price, request=dict(request),
                )

            message = f"{info.name} ({result.retcode}): {info.description}{broker_comment}"

            if result.retcode == TRADE_RETCODE_INVALID_FILL and fills:
                request["type_filling"] = fills.pop(0)
                logger.warning("%s: %s; retrying with filling mode %s", label, message, request["type_filling"])
                continue

            if info.outcome == "retry" and attempt <= self.max_retries:
                if is_market:
                    tick = self.connector.get_tick(request["symbol"])
                    request["price"] = tick.ask if side == "BUY" else tick.bid
                logger.warning("%s: %s; retrying", label, message)
                continue

            logger.error("%s rejected: %s", label, message)
            return OrderResult(False, result.retcode, info.name, message, request=dict(request))

        # Only reachable when INVALID_FILL retries exhaust the attempt budget.
        return OrderResult(False, TRADE_RETCODE_INVALID_FILL, "TRADE_RETCODE_INVALID_FILL",
                           "No accepted filling mode", request=dict(request))

    def _validate_stops(
        self,
        spec: SymbolSpec,
        tick: Tick | None,
        side: Side,
        price: float,
        stop_loss: float,
        take_profit: float | None,
    ) -> tuple[float, float | None]:
        if not stop_loss or stop_loss <= 0:
            raise OrderValidationError("A stop loss is required on every order")
        stop_loss = _normalize_price(stop_loss, spec)
        take_profit = _normalize_price(take_profit, spec) if take_profit else None

        if side == "BUY":
            if stop_loss >= price:
                raise OrderValidationError(f"BUY stop loss {stop_loss} must be below entry {price}")
            if take_profit is not None and take_profit <= price:
                raise OrderValidationError(f"BUY take profit {take_profit} must be above entry {price}")
        else:
            if stop_loss <= price:
                raise OrderValidationError(f"SELL stop loss {stop_loss} must be above entry {price}")
            if take_profit is not None and take_profit >= price:
                raise OrderValidationError(f"SELL take profit {take_profit} must be below entry {price}")

        # For market orders stops are measured from the price they close at
        # (bid for buys, ask for sells); for pending orders, from the order price.
        if tick is not None:
            reference = tick.bid if side == "BUY" else tick.ask
            if (stop_loss >= reference) if side == "BUY" else (stop_loss <= reference):
                raise OrderValidationError(
                    f"{side} stop loss {stop_loss} is on the wrong side of the current "
                    f"{'bid' if side == 'BUY' else 'ask'} {reference}"
                )
        else:
            reference = price
        min_distance = spec.stops_level * spec.point
        for name, level in (("stop loss", stop_loss), ("take profit", take_profit)):
            if level is not None and min_distance and abs(reference - level) < min_distance - 1e-12:
                raise OrderValidationError(
                    f"{name} {level} is closer than the broker's stops level "
                    f"({spec.stops_level} points = {min_distance}) to {reference}"
                )

        if spec.trade_mode == SYMBOL_TRADE_MODE_DISABLED:
            raise OrderValidationError(f"Trading is disabled for {spec.name}")
        if spec.trade_mode == SYMBOL_TRADE_MODE_CLOSEONLY:
            raise OrderValidationError(f"{spec.name} is close-only; new positions are not allowed")
        if spec.trade_mode == SYMBOL_TRADE_MODE_LONGONLY and side == "SELL":
            raise OrderValidationError(f"{spec.name} is long-only")
        if spec.trade_mode == SYMBOL_TRADE_MODE_SHORTONLY and side == "BUY":
            raise OrderValidationError(f"{spec.name} is short-only")
        return stop_loss, take_profit

    def _resolve_volume(
        self,
        spec: SymbolSpec,
        price: float,
        stop_loss: float,
        volume: float | None,
        risk_percent: float | None,
    ) -> float:
        if volume is None:
            return self.calculate_lot_size(price, stop_loss, spec.name, risk_percent)
        if not spec.volume_min <= volume <= spec.volume_max:
            raise OrderValidationError(
                f"Volume {volume} outside allowed range {spec.volume_min}-{spec.volume_max}"
            )
        steps = volume / spec.volume_step
        if abs(steps - round(steps)) > 1e-6:
            raise OrderValidationError(f"Volume {volume} is not a multiple of step {spec.volume_step}")
        return round(volume, _decimals(spec.volume_step))

    def _check_margin(self, order_type: int, symbol: str, volume: float, price: float) -> None:
        required = self.mt5.order_calc_margin(order_type, symbol, volume, price)
        if required is None:
            code, description = self._last_error()
            logger.warning("Could not pre-check margin: [%s] %s", code, description)
            return
        free = self.connector.account_info().margin_free
        if required > free:
            message = (
                f"Insufficient margin for {volume} {symbol}: requires {required:.2f}, "
                f"free margin {free:.2f}"
            )
            logger.error(message)
            raise InsufficientMarginError(message)

    def _market_filling_modes(self, spec: SymbolSpec) -> list[int]:
        modes = []
        if spec.filling_mode & SYMBOL_FILLING_FOK:
            modes.append(self.mt5.ORDER_FILLING_FOK)
        if spec.filling_mode & SYMBOL_FILLING_IOC:
            modes.append(self.mt5.ORDER_FILLING_IOC)
        modes.append(self.mt5.ORDER_FILLING_RETURN)
        return modes

    def _last_error(self) -> tuple[int, str]:
        try:
            code, description = self.mt5.last_error()
            return code, description
        except Exception:  # pragma: no cover - defensive
            return -1, "unknown error"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _validate_side(side: str) -> Side:
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise OrderValidationError(f"side must be BUY or SELL, got {side!r}")
    return side  # type: ignore[return-value]


def _normalize_price(price: float, spec: SymbolSpec) -> float:
    tick = spec.tick_size or spec.point
    return round(round(price / tick) * tick, spec.digits)


def _decimals(step: float) -> int:
    text = f"{step:.10f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0
