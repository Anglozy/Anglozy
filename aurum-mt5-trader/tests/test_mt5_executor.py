import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from config.settings import TradingSettings
from mt5.connector import MT5Connector
from mt5.executor import (
    InsufficientMarginError,
    MT5Executor,
    OrderValidationError,
    RiskTooSmallError,
    lot_size_for_risk,
)
from tests.fake_mt5 import FakeMT5

MAGIC = 777


@pytest.fixture
def fake():
    return FakeMT5()


@pytest.fixture
def executor(fake):
    connector = MT5Connector(trading=TradingSettings(magic_number=MAGIC), mt5_module=fake, retry_delay=0)
    connector.connect()
    return MT5Executor(connector)


# ---------------------------------------------------------------- sizing
@pytest.mark.parametrize(
    ("risk", "distance", "expected"),
    [
        (100, 7.30, 0.13),   # 7.30 stop = 730 USD/lot -> 0.137
        (100, 10.0, 0.10),
        (500, 5.0, 1.00),
        (1_000_000, 1.0, 100.0),  # capped at volume_max
    ],
)
def test_lot_size_for_risk(risk, distance, expected):
    lots = lot_size_for_risk(risk, distance, tick_size=0.01, tick_value=1.0,
                             volume_min=0.01, volume_max=100.0, volume_step=0.01)
    assert lots == expected


def test_lot_size_below_minimum_is_refused():
    with pytest.raises(RiskTooSmallError, match="below the minimum"):
        lot_size_for_risk(1, 10.0, 0.01, 1.0, 0.01, 100.0, 0.01)


def test_calculate_lot_size_uses_balance_and_risk_percent(executor):
    assert executor.calculate_lot_size(2648.50, 2641.20) == 0.13  # 1% of 10k
    assert executor.calculate_lot_size(2648.50, 2641.20, risk_percent=2) == 0.27


# ---------------------------------------------------------------- market orders
def test_market_buy_success(executor, fake):
    fake.send_results = [{"retcode": 10009, "order": 111, "deal": 222}]

    result = executor.place_market_order("buy", stop_loss=2642.00, take_profit=2665.00)

    assert result.success and result.retcode_name == "TRADE_RETCODE_DONE"
    assert (result.order, result.deal) == (111, 222)
    request = fake.sent[0]
    assert request["action"] == FakeMT5.TRADE_ACTION_DEAL
    assert request["type"] == FakeMT5.ORDER_TYPE_BUY
    assert request["price"] == 2650.30  # ask
    assert (request["sl"], request["tp"]) == (2642.00, 2665.00)
    assert request["magic"] == MAGIC
    assert request["type_filling"] == FakeMT5.ORDER_FILLING_FOK
    assert request["volume"] == 0.12  # 100 USD / (8.30 * 100)


def test_market_sell_uses_bid(executor, fake):
    fake.send_results = [{"retcode": 10009}]
    executor.place_market_order("SELL", stop_loss=2658.00, volume=0.5)
    assert fake.sent[0]["price"] == 2650.00
    assert fake.sent[0]["type"] == FakeMT5.ORDER_TYPE_SELL
    assert fake.sent[0]["volume"] == 0.5


@pytest.mark.parametrize(
    ("retcode", "name"),
    [
        (10019, "TRADE_RETCODE_NO_MONEY"),
        (10018, "TRADE_RETCODE_MARKET_CLOSED"),
        (10027, "TRADE_RETCODE_CLIENT_DISABLES_AT"),
        (10016, "TRADE_RETCODE_INVALID_STOPS"),
    ],
)
def test_fatal_retcodes_fail_without_retry(executor, fake, caplog, retcode, name):
    fake.send_results = [{"retcode": retcode, "comment": "broker says no"}]

    with caplog.at_level(logging.ERROR):
        result = executor.place_market_order("BUY", stop_loss=2642.00)

    assert not result.success
    assert result.retcode == retcode and result.retcode_name == name
    assert "broker says no" in result.message
    assert len(fake.sent) == 1
    assert name in caplog.text


def test_requote_is_retried_with_fresh_price(executor, fake):
    fake.send_results = [{"retcode": 10004}, {"retcode": 10009}]

    def move_price(*_):
        fake.tick.ask = 2650.80
        return None

    original_send = fake.order_send

    def send(request):
        result = original_send(request)
        move_price()
        return result

    fake.order_send = send

    result = executor.place_market_order("BUY", stop_loss=2642.00, volume=0.1)

    assert result.success
    assert [r["price"] for r in fake.sent] == [2650.30, 2650.80]


def test_retries_are_bounded(executor, fake):
    fake.send_results = [{"retcode": 10031}] * 5
    result = executor.place_market_order("BUY", stop_loss=2642.00, volume=0.1)
    assert not result.success and result.retcode_name == "TRADE_RETCODE_CONNECTION"
    assert len(fake.sent) == 3  # 1 + max_retries(2)


def test_invalid_fill_switches_filling_mode(executor, fake):
    fake.symbols["XAUUSD"].filling_mode = 3  # FOK and IOC allowed
    fake.send_results = [{"retcode": 10030}, {"retcode": 10009}]

    result = executor.place_market_order("BUY", stop_loss=2642.00, volume=0.1)

    assert result.success
    assert [r["type_filling"] for r in fake.sent] == [FakeMT5.ORDER_FILLING_FOK, FakeMT5.ORDER_FILLING_IOC]


def test_no_result_reports_last_error(executor, fake):
    fake.send_results = [None, None, None]
    fake.error = (-10004, "No IPC connection")
    result = executor.place_market_order("BUY", stop_loss=2642.00, volume=0.1)
    assert not result.success
    assert "No IPC connection" in result.message


def test_insufficient_margin_blocks_order(executor, fake):
    fake.margin = 50_000.0
    with pytest.raises(InsufficientMarginError, match="requires 50000.00, free margin 9000.00"):
        executor.place_market_order("BUY", stop_loss=2642.00, volume=5)
    assert fake.sent == []


@pytest.mark.parametrize(
    ("side", "sl", "tp", "match"),
    [
        ("BUY", 0, None, "stop loss is required"),
        ("BUY", 2655.00, None, "must be below entry"),
        ("BUY", 2650.10, None, "wrong side of the current bid"),  # between bid and ask
        ("BUY", 2642.00, 2645.00, "take profit .* must be above"),
        ("SELL", 2640.00, None, "must be above entry"),
        ("HOLD", 2640.00, None, "BUY or SELL"),
    ],
)
def test_invalid_stops_are_rejected_locally(executor, fake, side, sl, tp, match):
    with pytest.raises(OrderValidationError, match=match):
        executor.place_market_order(side, stop_loss=sl, take_profit=tp, volume=0.1)
    assert fake.sent == []


def test_stops_level_enforced(executor, fake):
    fake.symbols["XAUUSD"].trade_stops_level = 200  # 2.00 in price
    with pytest.raises(OrderValidationError, match="stops level"):
        executor.place_market_order("BUY", stop_loss=2649.00, volume=0.1)


def test_volume_must_match_step(executor):
    with pytest.raises(OrderValidationError, match="multiple of step"):
        executor.place_market_order("BUY", stop_loss=2642.00, volume=0.015)


def test_close_only_symbol_rejected(executor, fake):
    fake.symbols["XAUUSD"].trade_mode = 3
    with pytest.raises(OrderValidationError, match="close-only"):
        executor.place_market_order("BUY", stop_loss=2642.00, volume=0.1)


# ---------------------------------------------------------------- limit orders
def test_buy_limit_placed(executor, fake):
    fake.send_results = [{"retcode": 10008, "order": 333}]

    result = executor.place_limit_order("BUY", price=2648.504, stop_loss=2641.20, take_profit=2663.10)

    assert result.success and result.retcode_name == "TRADE_RETCODE_PLACED"
    request = fake.sent[0]
    assert request["action"] == FakeMT5.TRADE_ACTION_PENDING
    assert request["type"] == FakeMT5.ORDER_TYPE_BUY_LIMIT
    assert request["price"] == 2648.50  # normalised to tick size
    assert request["type_filling"] == FakeMT5.ORDER_FILLING_RETURN
    assert request["type_time"] == FakeMT5.ORDER_TIME_GTC
    assert request["volume"] == 0.13
    assert request["magic"] == MAGIC


def test_sell_limit_with_expiration(executor, fake):
    fake.send_results = [{"retcode": 10008}]
    expiry = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    executor.place_limit_order("SELL", price=2660.00, stop_loss=2668.00, volume=0.2, expiration=expiry)

    request = fake.sent[0]
    assert request["type"] == FakeMT5.ORDER_TYPE_SELL_LIMIT
    assert request["type_time"] == FakeMT5.ORDER_TIME_SPECIFIED
    assert request["expiration"] == int(expiry.timestamp())


def test_limit_on_wrong_side_of_market_rejected(executor):
    with pytest.raises(OrderValidationError, match="Buy limit .* must be below the ask"):
        executor.place_limit_order("BUY", price=2655.00, stop_loss=2645.00, volume=0.1)
    with pytest.raises(OrderValidationError, match="Sell limit .* must be above the bid"):
        executor.place_limit_order("SELL", price=2645.00, stop_loss=2655.00, volume=0.1)


# ---------------------------------------------------------------- magic filtering
def test_positions_and_orders_filtered_by_magic(executor, fake):
    fake.positions = [SimpleNamespace(symbol="XAUUSD", magic=MAGIC, ticket=1),
                      SimpleNamespace(symbol="XAUUSD", magic=1, ticket=2)]
    fake.orders = [SimpleNamespace(symbol="XAUUSD", magic=MAGIC, ticket=3)]

    assert [p.ticket for p in executor.open_positions("XAUUSD")] == [1]
    assert [o.ticket for o in executor.pending_orders()] == [3]
