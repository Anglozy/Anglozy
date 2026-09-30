from types import SimpleNamespace

import pytest

import main
from tests.fake_mt5 import FakeMT5
from tests.scenarios import BULLISH_ASK, BULLISH_BID, htf_bullish, ltf_bullish, to_rates


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "AURUM_SYMBOL", "AURUM_ALLOW_LIVE_TRADING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("mt5.connector.time.sleep", lambda _: None)


@pytest.fixture
def fake():
    fake = FakeMT5()
    fake.rates = {
        FakeMT5.TIMEFRAME_H4: to_rates(htf_bullish()),
        FakeMT5.TIMEFRAME_M15: to_rates(ltf_bullish()),
    }
    fake.tick = SimpleNamespace(time=1_790_000_000, bid=BULLISH_BID, ask=BULLISH_ASK, last=0.0)
    return fake


def run_main(fake, tmp_path, *extra):
    return main.main(["--utc-offset", "0", "--reports-dir", str(tmp_path), *extra], mt5_module=fake)


def test_main_runs_full_flow(fake, tmp_path, capsys):
    fake.send_results = [{"retcode": 10008, "order": 777}]

    code = run_main(fake, tmp_path)

    assert code == main.EXIT_OK
    [request] = fake.sent
    assert request["symbol"] == "XAUUSD"
    assert (request["price"], request["sl"], request["tp"], request["volume"]) == (2646.5, 2636.5, 2672.0, 0.1)
    assert len(list(tmp_path.glob("aurum_XAUUSD_BUY_*.html"))) == 1
    out = capsys.readouterr().out
    assert "EXECUTED" in out and "Lot size: 0.1" in out
    assert fake.shutdown_calls == 1


def test_main_dry_run(fake, tmp_path, capsys):
    assert run_main(fake, tmp_path, "--dry-run") == main.EXIT_OK
    assert fake.sent == []
    assert len(list(tmp_path.glob("*.html"))) == 1
    assert "REPORTED" in capsys.readouterr().out


def test_main_order_rejection_exit_code(fake, tmp_path):
    fake.send_results = [{"retcode": 10019}]  # no money
    assert run_main(fake, tmp_path) == main.EXIT_ORDER_FAILED


def test_main_connection_failure(fake, tmp_path):
    fake.init_results = [False, False, False]
    assert run_main(fake, tmp_path) == main.EXIT_CONNECTION
    assert fake.sent == []


def test_main_refuses_real_account(fake, tmp_path):
    fake.account.trade_mode = 2
    assert run_main(fake, tmp_path) == main.EXIT_CONNECTION
