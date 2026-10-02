import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from api.state import (
    PASSWORD_MASK,
    BotStateManager,
    BotStatus,
    CredentialStore,
    PipelineRunner,
    StateError,
    detector_stats,
)
from config.settings import MT5Credentials, Settings, TradingSettings, load_settings
from config.strategy import StrategyParameters
from tests.fake_mt5 import FakeMT5
from tests.scenarios import BULLISH_ASK, BULLISH_BID, htf_bullish, ltf_bullish, to_rates

MAGIC = 20261001
CRED_KEYS = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class FakeRunner:
    """Runner stand-in: counts calls, can fail on demand."""

    instances: list["FakeRunner"] = []

    def __init__(self, config, fail_start=None, fail_run=None):
        self.config = config
        self.fail_start = fail_start
        self.fail_run = fail_run
        self.runs = 0
        self.closed = False
        FakeRunner.instances.append(self)

    def start(self):
        if self.fail_start:
            raise RuntimeError(self.fail_start)
        return {"login": 35068828, "balance": 10_000.0, "equity": 10_050.0}

    def run_once(self):
        self.runs += 1
        if self.fail_run:
            raise RuntimeError(self.fail_run)
        return {
            "result": {"status": "no_setup", "message": f"pass {self.runs}"},
            "detectors": {"htf_bias": "Bullish"},
            "account": {"login": 35068828, "balance": 10_000.0, "equity": 10_000.0 + self.runs},
        }

    def positions(self):
        return {"symbol": "XAUUSD", "magic": MAGIC, "positions": [{"ticket": 1}], "orders": []}

    def close_ticket(self, ticket):
        self.closed_tickets = getattr(self, "closed_tickets", []) + [ticket]
        return {"action": "close", "success": True, "ticket": ticket, "message": "Request completed"}

    def close(self):
        self.closed = True


def settings_loader():
    return Settings(MT5Credentials(), TradingSettings(magic_number=MAGIC))


@pytest.fixture(autouse=True)
def isolate_env():
    saved = {k: os.environ.get(k) for k in CRED_KEYS}
    for k in CRED_KEYS:
        os.environ.pop(k, None)
    FakeRunner.instances.clear()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def paths(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    return SimpleNamespace(env=tmp_path / ".env", reports=reports)


def make_client(paths, runner_factory=FakeRunner, token=None, interval=0.01):
    state = BotStateManager(runner_factory=runner_factory, default_interval=interval)
    app = create_app(
        state=state,
        credentials=CredentialStore(paths.env),
        reports_dir=paths.reports,
        settings_loader=settings_loader,
        api_token=token,
    )
    return TestClient(app), state


@pytest.fixture
def client(paths):
    client, state = make_client(paths)
    with client:
        client.bot = state
        yield client


def control(client, action, **body):
    return client.post("/api/control", json={"action": action, **body})


# --------------------------------------------------------------------------- #
# Status and control
# --------------------------------------------------------------------------- #
def test_initial_status(client):
    body = client.get("/api/status").json()
    assert body["status"] == "stopped"
    assert body["connected"] is False
    assert body["runs"] == 0
    assert body["trading"]["symbol"] == "XAUUSD"
    assert body["trading"]["magic_number"] == MAGIC


def test_start_runs_in_background(client):
    r = control(client, "start", interval_seconds=0.01)
    assert r.status_code == 200
    assert r.json()["status"] in ("starting", "scanning")
    assert client.bot.wait_for_runs(2)

    body = client.get("/api/status").json()
    assert body["status"] == "scanning"
    assert body["connected"] is True
    assert body["dry_run"] is True  # safe default
    assert body["runs"] >= 2
    assert body["account"]["login"] == 35068828
    assert body["last_result"]["status"] == "no_setup"
    assert FakeRunner.instances[0].config.dry_run is True


def test_start_live_mode_passes_config(client):
    control(client, "start", dry_run=False, interval_seconds=5)
    assert client.bot.wait_for_status(BotStatus.SCANNING)
    assert FakeRunner.instances[0].config.dry_run is False
    assert FakeRunner.instances[0].config.interval_seconds == 5


def test_pause_stops_scanning_and_resume_continues(client):
    control(client, "start")
    assert client.bot.wait_for_runs(1)
    assert control(client, "pause").json()["status"] == "paused"

    time.sleep(0.05)  # let an in-flight pass finish
    runs = client.bot.runs
    time.sleep(0.15)
    assert client.bot.runs == runs  # no passes while paused
    assert client.get("/api/status").json()["connected"] is True  # session stays open

    assert control(client, "resume").json()["status"] == "scanning"
    assert client.bot.wait_for_runs(runs + 2)


def test_toggle_cycles_states(client):
    assert control(client, "toggle").json()["status"] in ("starting", "scanning")
    assert client.bot.wait_for_status(BotStatus.SCANNING)
    assert control(client, "toggle").json()["status"] == "paused"
    assert control(client, "toggle").json()["status"] == "scanning"


def test_stop_closes_session(client):
    control(client, "start")
    assert client.bot.wait_for_runs(1)
    body = control(client, "stop").json()
    assert body["status"] == "stopped"
    assert body["connected"] is False
    assert FakeRunner.instances[0].closed


@pytest.mark.parametrize(("setup", "action"), [
    ([], "pause"),
    ([], "resume"),
    (["start"], "start"),
    (["start"], "resume"),
])
def test_invalid_transitions_conflict(client, setup, action):
    for step in setup:
        control(client, step)
        client.bot.wait_for_status(BotStatus.SCANNING)
    r = control(client, action)
    assert r.status_code == 409


@pytest.mark.parametrize("body", [
    {"action": "launch"},
    {"action": "start", "interval_seconds": 0},
    {"action": "start", "interval_seconds": -5},
])
def test_invalid_control_requests(client, body):
    assert client.post("/api/control", json=body).status_code == 422


def test_start_failure_sets_error_and_can_retry(paths):
    attempts = []

    def factory(config):
        attempts.append(config)
        return FakeRunner(config, fail_start="MT5 login failed" if len(attempts) == 1 else None)

    client, state = make_client(paths, runner_factory=factory)
    with client:
        control(client, "start")
        assert state.wait_for_status(BotStatus.ERROR)
        body = client.get("/api/status").json()
        assert body["status"] == "error"
        assert "MT5 login failed" in body["last_error"]
        assert FakeRunner.instances[0].closed

        assert control(client, "start").status_code == 200  # retry allowed after error
        assert state.wait_for_runs(1)
        assert client.get("/api/status").json()["last_error"] is None


def test_failed_pass_is_recorded_and_bot_keeps_scanning(paths):
    client, state = make_client(paths, runner_factory=lambda cfg: FakeRunner(cfg, fail_run="tick timeout"))
    with client:
        control(client, "start")
        assert state.wait_for_runs(2)
        body = client.get("/api/status").json()
        assert body["status"] == "scanning"
        assert "tick timeout" in body["last_error"]


def test_shutdown_stops_background_thread(paths):
    client, state = make_client(paths)
    with client:
        control(client, "start")
        assert state.wait_for_runs(1)
    assert state.status == BotStatus.STOPPED
    assert not any(t.name == "aurum-scanner" for t in threading.enumerate())


# --------------------------------------------------------------------------- #
# Dashboard page
# --------------------------------------------------------------------------- #
def test_dashboard_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "<title>Aurum Dashboard</title>" in html
    for marker in ("#0D0D0D", "#D4AF37", 'id="toggle-btn"', 'id="creds-form"', 'id="tv-chart"',
                   'id="trades"', 'id="reports"', "POLL_MS = 3000"):
        assert marker in html
    for endpoint in ("/api/status", "/api/positions", "/api/signal", "/api/control",
                     "/api/credentials", "/api/reports", "/close"):
        assert endpoint in html


def test_status_includes_tradingview_symbol(client):
    assert client.get("/api/status").json()["trading"]["tv_symbol"] == "OANDA:XAUUSD"


def test_close_endpoint_uses_runner(client):
    assert client.post("/api/positions/5/close").status_code == 503  # not connected yet
    control(client, "start")
    assert client.bot.wait_for_status(BotStatus.SCANNING)
    r = client.post("/api/positions/5/close")
    assert r.status_code == 200 and r.json()["ticket"] == 5
    assert FakeRunner.instances[0].closed_tickets == [5]


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def test_token_protects_control_and_credentials(paths):
    client, _ = make_client(paths, token="s3cret")
    with client:
        assert client.get("/api/status").status_code == 200  # read-only status stays open
        assert control(client, "start").status_code == 401
        assert client.get("/api/credentials").status_code == 401
        assert client.post("/api/control", json={"action": "start"},
                           headers={"Authorization": "Bearer wrong"}).status_code == 401

        auth = {"Authorization": "Bearer s3cret"}
        assert client.post("/api/control", json={"action": "start"}, headers=auth).status_code == 200
        assert client.get("/api/credentials", headers=auth).status_code == 200
        assert client.post("/api/positions/1/close").status_code == 401
        assert client.get("/").status_code == 200  # the page itself loads; it asks for the token


@pytest.mark.parametrize("token", ["", "   "])
def test_blank_token_disables_auth(paths, token):
    client, _ = make_client(paths, token=token)
    with client:
        assert control(client, "start").status_code == 200


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
def test_positions_unavailable_when_stopped(client):
    r = client.get("/api/positions")
    assert r.status_code == 503
    assert "not connected" in r.json()["detail"]


def test_positions_from_runner(client):
    control(client, "start")
    assert client.bot.wait_for_status(BotStatus.SCANNING)
    body = client.get("/api/positions").json()
    assert body["magic"] == MAGIC and body["positions"] == [{"ticket": 1}]


# --------------------------------------------------------------------------- #
# Signal
# --------------------------------------------------------------------------- #
def test_signal_before_first_pass(client):
    body = client.get("/api/signal").json()
    assert body == {"status": "stopped", "last_run_at": None, "last_result": None, "detectors": None}


def test_signal_after_pass(client):
    control(client, "start")
    assert client.bot.wait_for_runs(1)
    body = client.get("/api/signal").json()
    assert body["detectors"] == {"htf_bias": "Bullish"}
    assert body["last_result"]["status"] == "no_setup"
    assert body["last_run_at"] is not None


# --------------------------------------------------------------------------- #
# Full stack: real PipelineRunner on the simulated MT5 terminal
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_terminal():
    t = FakeMT5()
    t.rates = {
        FakeMT5.TIMEFRAME_H4: to_rates(htf_bullish()),
        FakeMT5.TIMEFRAME_M15: to_rates(ltf_bullish()),
    }
    t.tick = SimpleNamespace(time=1_790_000_000, bid=BULLISH_BID, ask=BULLISH_ASK, last=0.0)
    position = dict(symbol="XAUUSD", type=0, volume=0.1, price_open=2646.5, price_current=2654.0,
                    sl=2636.5, tp=2672.0, profit=75.0, swap=0.0, time=1_790_000_000, comment="aurum ict")
    t.positions = [
        SimpleNamespace(ticket=111, magic=MAGIC, **position),
        SimpleNamespace(ticket=222, magic=12345, **position),  # someone else's trade
    ]
    t.orders = [SimpleNamespace(ticket=333, magic=MAGIC, symbol="XAUUSD", type=3, volume_current=0.2,
                                price_open=2660.0, sl=2668.0, tp=2640.0, time_setup=1_790_000_100, comment="")]
    return t


def test_full_pipeline_through_api(paths, fake_terminal):
    def factory(config):
        return PipelineRunner(
            config, settings_loader=settings_loader, params=StrategyParameters(server_utc_offset_hours=0),
            reports_dir=paths.reports, mt5_module=fake_terminal, charts=False,
        )

    client, state = make_client(paths, runner_factory=factory)
    with client:
        control(client, "start", interval_seconds=30)
        assert state.wait_for_runs(1, timeout=10)

        status = client.get("/api/status").json()
        assert status["status"] == "scanning"
        assert status["account"]["balance"] == 10_000.0
        assert status["account"]["trade_mode"] == "DEMO"
        # existing magic-20261001 exposure means the pipeline will not stack a new trade
        assert status["last_result"]["status"] == "skipped"

        positions = client.get("/api/positions").json()
        assert positions["magic"] == MAGIC
        assert [p["ticket"] for p in positions["positions"]] == [111]
        assert positions["positions"][0]["type"] == "BUY"
        assert positions["positions"][0]["profit"] == 75.0
        assert [(o["ticket"], o["type"]) for o in positions["orders"]] == [(333, "SELL_LIMIT")]

        signal = client.get("/api/signal").json()
        d = signal["detectors"]
        assert d["htf_bias"] == "Bullish"
        assert d["fvgs"]["unfilled"]["bullish"] >= 1
        assert any(s["session"] == "Asia" and s["side"] == "low" for s in d["sweeps"]["latest"])
        assert signal["last_result"]["signal"]["entry"] == 2646.5
        json.dumps(signal)  # fully serialisable


def test_close_and_cancel_through_api(paths, fake_terminal):
    def factory(config):
        return PipelineRunner(
            config, settings_loader=settings_loader, params=StrategyParameters(server_utc_offset_hours=0),
            reports_dir=paths.reports, mt5_module=fake_terminal, charts=False,
        )

    client, state = make_client(paths, runner_factory=factory)
    with client:
        control(client, "start", interval_seconds=30)
        assert state.wait_for_runs(1, timeout=10)
        fake_terminal.sent.clear()
        fake_terminal.send_results = [{"retcode": 10009, "deal": 9}, {"retcode": 10009}]

        r = client.post("/api/positions/111/close")
        assert r.status_code == 200
        assert r.json()["action"] == "close" and r.json()["success"] is True
        close_req = fake_terminal.sent[-1]
        assert (close_req["position"], close_req["type"], close_req["price"]) == (111, FakeMT5.ORDER_TYPE_SELL, BULLISH_BID)

        r = client.post("/api/positions/333/close")
        assert r.status_code == 200 and r.json()["action"] == "cancel"
        assert fake_terminal.sent[-1] == {"action": FakeMT5.TRADE_ACTION_REMOVE, "order": 333}

        # someone else's trade and unknown tickets are refused without sending anything
        sent = len(fake_terminal.sent)
        assert client.post("/api/positions/222/close").status_code == 404
        assert client.post("/api/positions/999/close").status_code == 404
        assert len(fake_terminal.sent) == sent

        fake_terminal.send_results = [{"retcode": 10018}]  # market closed
        r = client.post("/api/positions/111/close")
        assert r.status_code == 502 and "MARKET_CLOSED" in r.json()["detail"]


def test_full_pipeline_generates_report_listed_by_api(paths, fake_terminal):
    fake_terminal.positions, fake_terminal.orders = [], []

    def factory(config):
        return PipelineRunner(
            config, settings_loader=settings_loader, params=StrategyParameters(server_utc_offset_hours=0),
            reports_dir=paths.reports, mt5_module=fake_terminal, charts=False,
        )

    client, state = make_client(paths, runner_factory=factory)
    with client:
        control(client, "start", interval_seconds=30)
        assert state.wait_for_runs(1, timeout=10)
        result = client.get("/api/signal").json()["last_result"]
        assert result["status"] == "reported"  # dry run: no order sent
        assert result["lot_size"] == 0.1
        assert fake_terminal.sent == []

        reports = client.get("/api/reports").json()["reports"]
        assert [r["name"] for r in reports] == [result["report"]]
        page = client.get(reports[0]["url"])
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "<title>Aurum Report · XAUUSD BUY</title>" in page.text


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def test_credentials_empty(client):
    body = client.get("/api/credentials").json()
    assert body["login"] is None and body["server"] is None
    assert body["password"] == "" and body["password_set"] is False


def test_credentials_update_masks_password(client, paths):
    r = client.post("/api/credentials", json={
        "login": 35068828, "server": "FundedNext-Server 3", "password": 'p@ss#w"rd',
    })
    assert r.status_code == 200
    body = r.json()
    assert body["login"] == 35068828
    assert body["server"] == "FundedNext-Server 3"
    assert body["password"] == PASSWORD_MASK and body["password_set"] is True
    assert body["restart_required"] is False
    assert "p@ss" not in r.text
    assert "p@ss" not in client.get("/api/credentials").text

    content = paths.env.read_text()
    assert "MT5_LOGIN=35068828" in content
    assert 'MT5_SERVER="FundedNext-Server 3"' in content


def test_saved_credentials_load_back_through_settings(client, paths):
    password = 'qw#r"OK\\61 ##'
    client.post("/api/credentials", json={"login": 35068828, "server": "FundedNext-Server 3", "password": password})
    for k in CRED_KEYS:
        os.environ.pop(k, None)  # force load_settings to read the file

    creds = load_settings(env_file=paths.env).credentials
    assert creds.login == 35068828
    assert creds.server == "FundedNext-Server 3"
    assert creds.password == password


def test_credentials_update_sets_process_environment(client):
    client.post("/api/credentials", json={"login": 123, "server": "Demo", "password": "x"})
    assert os.environ["MT5_LOGIN"] == "123"
    assert os.environ["MT5_SERVER"] == "Demo"


@pytest.mark.parametrize("keep", [None, "", PASSWORD_MASK])
def test_blank_or_masked_password_keeps_existing(client, paths, keep):
    client.post("/api/credentials", json={"login": 1, "server": "A", "password": "original"})
    client.post("/api/credentials", json={"login": 2, "server": "B", "password": keep})
    content = paths.env.read_text()
    assert "MT5_PASSWORD=original" in content
    assert "MT5_LOGIN=2" in content


def test_credentials_update_preserves_other_lines(client, paths):
    paths.env.write_text("# my settings\nAURUM_RISK_PERCENT=0.5\nMT5_LOGIN=1\nMT5_PASSWORD=old\nMT5_SERVER=Old\n")
    client.post("/api/credentials", json={"login": 999, "server": "New"})
    lines = paths.env.read_text().splitlines()
    assert lines[:2] == ["# my settings", "AURUM_RISK_PERCENT=0.5"]
    assert "MT5_LOGIN=999" in lines and "MT5_SERVER=New" in lines and "MT5_PASSWORD=old" in lines


@pytest.mark.parametrize("body", [
    {"login": 123, "server": "Demo"},                            # no password stored yet
    {"login": -1, "server": "Demo", "password": "x"},
    {"login": 123, "server": "Demo\nMT5_LOGIN=666", "password": "x"},  # line injection
    {"login": 123, "server": "", "password": "x"},
])
def test_invalid_credentials_rejected(client, paths, body):
    r = client.post("/api/credentials", json=body)
    assert r.status_code == 422
    assert not paths.env.exists()


def test_credentials_restart_required_while_running(client):
    control(client, "start")
    client.bot.wait_for_status(BotStatus.SCANNING)
    r = client.post("/api/credentials", json={"login": 1, "server": "A", "password": "x"})
    assert r.json()["restart_required"] is True


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def test_reports_listed_newest_first(client, paths):
    old = paths.reports / "aurum_XAUUSD_BUY_1.html"
    new = paths.reports / "aurum_XAUUSD_SELL_2.html"
    old.write_text("<html>old</html>")
    new.write_text("<html>new</html>")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    (paths.reports / "notes.txt").write_text("ignore me")
    (paths.reports / "images").mkdir()

    reports = client.get("/api/reports").json()["reports"]
    assert [r["name"] for r in reports] == [new.name, old.name]
    assert reports[0]["url"] == f"/reports/{new.name}"
    assert client.get(reports[0]["url"]).text == "<html>new</html>"


@pytest.mark.parametrize("name", ["missing.html", "..%2F.env", "%2E%2E%2Fsecret.html", "notes.txt", ".hidden.html"])
def test_report_access_is_restricted(client, paths, name):
    (paths.env).write_text("MT5_PASSWORD=secret\n")
    (paths.reports / "notes.txt").write_text("x")
    r = client.get(f"/reports/{name}")
    assert r.status_code == 404
    assert "secret" not in r.text


# --------------------------------------------------------------------------- #
# State manager directly
# --------------------------------------------------------------------------- #
def test_concurrent_control_is_consistent():
    state = BotStateManager(runner_factory=FakeRunner, default_interval=0.005)
    errors = []

    def hammer():
        for _ in range(25):
            try:
                state.toggle()
            except StateError:
                pass
            except Exception as exc:  # anything else is a bug
                errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(t.name == "aurum-scanner" and t.is_alive() for t in threading.enumerate()) <= 1
    state.stop()
    assert state.status == BotStatus.STOPPED
    assert len(FakeRunner.instances) == 1  # toggles never raced into a second start
    assert FakeRunner.instances[0].closed


def test_stop_when_already_stopped_is_harmless():
    state = BotStateManager(runner_factory=FakeRunner)
    assert state.stop()["status"] == "stopped"


def test_detector_stats_on_scenario():
    stats = detector_stats(htf_bullish(), ltf_bullish(), StrategyParameters(), utc_offset_hours=0)
    assert stats["htf_bias"] == "Bullish"
    assert stats["htf_last_break"]["kind"] == "CHoCH"
    assert stats["fvgs"]["latest"][-1]["low"] == 2645.0
    assert stats["order_blocks"]["unmitigated"]["bullish"] >= 1
    assert stats["sweeps"]["latest"][0]["extreme"] == 2637.0
    json.dumps(stats)
