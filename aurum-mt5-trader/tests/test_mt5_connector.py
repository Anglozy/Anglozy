import pandas as pd
import pytest

from config.settings import MT5Credentials, TradingSettings, load_settings
from mt5.connector import MT5ConnectionError, MT5Connector, MT5DataError
from tests.fake_mt5 import FakeMT5


@pytest.fixture
def fake():
    return FakeMT5()


def make_connector(fake, credentials=None, trading=None):
    return MT5Connector(credentials, trading, mt5_module=fake, retry_delay=0)


def test_connect_logs_in_with_credentials(fake):
    creds = MT5Credentials(login=5012345, password="secret", server="Broker-Demo", path="C:/mt5/terminal64.exe")

    account = make_connector(fake, creds).connect()

    assert fake.init_calls == [{
        "timeout": 60_000, "path": "C:/mt5/terminal64.exe",
        "login": 5012345, "password": "secret", "server": "Broker-Demo",
    }]
    assert account.login == 5012345
    assert account.trade_mode == "DEMO"


def test_connect_without_login_attaches_to_running_terminal(fake):
    make_connector(fake).connect()
    assert fake.init_calls == [{"timeout": 60_000}]


def test_connect_retries_initialize(fake):
    fake.init_results = [False, False, True]
    make_connector(fake).connect()
    assert len(fake.init_calls) == 3


def test_connect_raises_after_all_attempts_fail(fake):
    fake.init_results = [False, False, False]
    fake.error = (-6, "Terminal: Authorization failed")

    with pytest.raises(MT5ConnectionError, match="Authorization failed"):
        make_connector(fake).connect()
    assert fake.shutdown_calls == 3


def test_connect_rejects_disconnected_terminal(fake):
    fake.terminal.connected = False
    with pytest.raises(MT5ConnectionError, match="not connected to the trade server"):
        make_connector(fake).connect()
    assert fake.shutdown_calls == 1


def test_real_account_requires_opt_in(fake):
    fake.account.trade_mode = 2
    with pytest.raises(MT5ConnectionError, match="REAL account"):
        make_connector(fake).connect()

    account = make_connector(fake, trading=TradingSettings(allow_live_trading=True)).connect()
    assert account.is_real


def test_missing_library_gives_clear_error():
    connector = MT5Connector(mt5_module=None)
    connector.mt5 = None  # simulate MetaTrader5 not installed
    with pytest.raises(MT5ConnectionError, match="not installed"):
        connector.connect()


def test_calls_before_connect_are_rejected(fake):
    with pytest.raises(MT5ConnectionError, match="Not connected"):
        make_connector(fake).get_tick("XAUUSD")


def test_context_manager_disconnects(fake):
    with make_connector(fake) as conn:
        assert conn.is_connected
    assert fake.shutdown_calls == 1
    assert not conn.is_connected


def test_get_tick(fake):
    with make_connector(fake) as conn:
        tick = conn.get_tick()

    assert tick.symbol == "XAUUSD"
    assert (tick.bid, tick.ask) == (2650.00, 2650.30)
    assert tick.spread_points == 30
    assert tick.time.tzinfo is not None


def test_get_tick_with_no_prices_reports_market_closed(fake):
    fake.tick.bid = fake.tick.ask = 0.0
    with make_connector(fake) as conn, pytest.raises(MT5DataError, match="market may be closed"):
        conn.get_tick()


def test_get_bars_returns_closed_bars_as_dataframe(fake):
    with make_connector(fake) as conn:
        bars = conn.get_bars("XAUUSD", "h4", count=50)

    assert fake.rates_calls == [("XAUUSD", FakeMT5.TIMEFRAME_H4, 1, 50)]
    assert len(bars) == 50
    assert list(bars.columns[:5]) == ["time", "open", "high", "low", "close"]
    assert isinstance(bars["time"].dtype, pd.DatetimeTZDtype)


def test_get_bars_can_include_forming_bar(fake):
    with make_connector(fake) as conn:
        conn.get_bars(timeframe="M15", count=10, include_current=True)
    assert fake.rates_calls[-1][2] == 0


def test_invalid_timeframe(fake):
    with make_connector(fake) as conn, pytest.raises(ValueError, match="Unsupported timeframe"):
        conn.get_bars(timeframe="M7")


def test_unknown_symbol(fake):
    with make_connector(fake) as conn, pytest.raises(MT5DataError, match="Unknown symbol"):
        conn.get_tick("GOLD")


def test_hidden_symbol_is_added_to_market_watch(fake):
    fake.symbols["XAUUSD"].visible = False
    with make_connector(fake) as conn:
        conn.get_tick()
    assert fake.selected == ["XAUUSD"]


# ---------------------------------------------------------------- settings
def test_load_settings_from_environment(monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "5012345")
    monkeypatch.setenv("MT5_PASSWORD", "secret")
    monkeypatch.setenv("MT5_SERVER", "Broker-Demo")
    monkeypatch.setenv("AURUM_RISK_PERCENT", "0.5")
    monkeypatch.setenv("AURUM_ALLOW_LIVE_TRADING", "true")

    settings = load_settings(env_file=None)

    assert settings.credentials.login == 5012345
    assert "secret" not in repr(settings.credentials)
    assert settings.trading.risk_percent == 0.5
    assert settings.trading.allow_live_trading is True


def test_load_settings_requires_password_with_login(monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "5012345")
    monkeypatch.delenv("MT5_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="MT5_PASSWORD and MT5_SERVER"):
        load_settings(env_file=None)
