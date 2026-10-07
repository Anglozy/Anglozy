"""In-memory stand-in for the ``MetaTrader5`` module so tests run without a terminal."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np


class FakeMT5:
    # Constants (values match the real MetaTrader5 package)
    TIMEFRAME_M1 = 1
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 2

    def __init__(self) -> None:
        self.init_results: list[bool] = []
        self.init_calls: list[dict] = []
        self.shutdown_calls = 0
        self.error = (1, "Success")
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True)
        self.account = SimpleNamespace(
            login=5012345, server="Broker-Demo", name="Test Trader", currency="USD",
            balance=10_000.0, equity=10_000.0, margin_free=9_000.0, leverage=100,
            trade_mode=0, trade_allowed=True,
        )
        self.symbols = {
            "XAUUSD": SimpleNamespace(
                name="XAUUSD", visible=True, digits=2, point=0.01,
                trade_tick_size=0.01, trade_tick_value=1.0, trade_tick_value_loss=1.0,
                trade_contract_size=100.0, volume_min=0.01, volume_max=100.0,
                volume_step=0.01, trade_stops_level=0, trade_freeze_level=0,
                filling_mode=1, trade_mode=4,
            )
        }
        self.tick = SimpleNamespace(time=1_790_000_000, bid=2650.00, ask=2650.30, last=0.0)
        self.selected: list[str] = []
        self.margin = 500.0
        self.send_results: list = []
        self.sent: list[dict] = []
        self.rates_calls: list[tuple] = []
        self.rates: dict[int, np.ndarray] = {}  # timeframe -> bars incl. forming bar
        self.positions: list = []
        self.orders: list = []
        self.deals: list = []  # history_deals_get results

    # ---- terminal
    def initialize(self, **kwargs):
        self.init_calls.append(kwargs)
        return self.init_results.pop(0) if self.init_results else True

    def shutdown(self):
        self.shutdown_calls += 1

    def last_error(self):
        return self.error

    def terminal_info(self):
        return self.terminal

    def account_info(self):
        return self.account

    # ---- symbols / data
    def symbol_info(self, symbol):
        return self.symbols.get(symbol)

    def symbol_select(self, symbol, enable):
        self.selected.append(symbol)
        self.symbols[symbol].visible = True
        return True

    def symbol_info_tick(self, symbol):
        return self.tick

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        self.rates_calls.append((symbol, timeframe, start, count))
        if timeframe in self.rates:
            arr = self.rates[timeframe]
            end = len(arr) - start
            return arr[max(0, end - count):end]
        dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
                 ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
        rows = [(1_790_000_000 + i * 900, 2650 + i, 2652 + i, 2649 + i, 2651 + i, 100, 30, 0)
                for i in range(count)]
        return np.array(rows, dtype=dtype)

    # ---- trading
    def order_calc_margin(self, order_type, symbol, volume, price):
        return self.margin

    def order_send(self, request):
        self.sent.append(dict(request))
        if not self.send_results:
            return None
        result = self.send_results.pop(0)
        if result is None:
            return None
        return SimpleNamespace(
            order=result.get("order", 0), deal=result.get("deal", 0),
            volume=request.get("volume", 0.0), price=request.get("price", 0.0), comment=result.get("comment", ""),
            retcode=result["retcode"],
        )

    def positions_get(self, symbol=None, ticket=None):
        return tuple(p for p in self.positions
                     if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket))

    def orders_get(self, symbol=None, ticket=None):
        return tuple(o for o in self.orders
                     if (symbol is None or o.symbol == symbol) and (ticket is None or o.ticket == ticket))

    def history_deals_get(self, date_from, date_to):
        lo, hi = date_from.timestamp(), date_to.timestamp()
        return tuple(d for d in self.deals if lo <= d.time <= hi)
