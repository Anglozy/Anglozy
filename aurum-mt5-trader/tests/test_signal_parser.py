from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from aurum.generator import AurumReportGenerator
from config.settings import TradingSettings
from config.strategy import StrategyParameters
from mt5.connector import MT5Connector
from mt5.executor import MT5Executor
from signals.parser import SignalPipeline, _to_tick, find_setup
from tests.fake_mt5 import FakeMT5
from tests.scenarios import (
    BULLISH_ASK,
    BULLISH_BID,
    EXPECTED_BUY,
    htf_bullish,
    ltf_bullish,
    make_bars,
    mirror,
    to_rates,
)

PARAMS = StrategyParameters(server_utc_offset_hours=0)
MAGIC = 4242


def bullish(params=PARAMS, bid=BULLISH_BID, ask=BULLISH_ASK, ltf=None, htf=None):
    return find_setup(
        htf if htf is not None else htf_bullish(),
        ltf if ltf is not None else ltf_bullish(),
        bid, ask, params, 0, tick_size=0.01,
    )


# ---------------------------------------------------------------- find_setup


def test_bullish_setup_levels():
    result = bullish()
    s = result.signal

    assert s is not None, result.reason
    assert s.direction == "BUY" and s.htf_bias == "Bullish"
    assert s.entry == EXPECTED_BUY["entry"]
    assert s.stop_loss == EXPECTED_BUY["stop_loss"]
    assert [t.price for t in s.targets] == EXPECTED_BUY["targets"]
    assert [t.label for t in s.targets] == ["H4 swing high", "H4 swing high"]
    assert s.primary_rr == pytest.approx(2.55)
    assert (s.sweep.session, s.sweep.side, s.sweep.extreme) == ("Asia", "low", 2637.0)
    assert (s.structure_break.kind, s.structure_break.level) == ("CHoCH", 2650.0)
    assert (s.fvg.low, s.fvg.high) == (2645.0, 2648.0)
    assert (s.order_block.low, s.order_block.high) == (2637.0, 2643.0)
    assert (s.htf_order_block.low, s.htf_order_block.high) == (2605.0, 2613.0)


def test_every_target_meets_min_rr():
    for min_rr in (2.0, 2.5, 3.0, 4.0):
        s = bullish(replace(PARAMS, min_rr=min_rr)).signal
        assert all(t.rr >= min_rr for t in s.targets)
        assert all(t.price > s.entry for t in s.targets)


def test_targets_below_min_rr_are_dropped():
    s = bullish(replace(PARAMS, min_rr=3.0)).signal
    assert [t.price for t in s.targets] == [2691.0]  # 2672 is only 2.55R


def test_fixed_target_when_no_liquidity_is_far_enough():
    s = bullish(replace(PARAMS, min_rr=5.0)).signal
    [target] = s.targets
    assert target.label == "Fixed 5R"
    assert target.price == 2646.50 + 5 * 10.0
    assert target.rr == pytest.approx(5.0)


def test_fvg_edge_entry():
    s = bullish(replace(PARAMS, entry_mode="fvg_edge")).signal
    assert s.entry == 2648.0  # top of the bullish FVG


def test_bearish_setup_mirrors_bullish():
    pivot = 2650.0
    result = find_setup(
        mirror(htf_bullish(), pivot), mirror(ltf_bullish(), pivot),
        bid=2 * pivot - BULLISH_ASK, ask=2 * pivot - BULLISH_BID,
        params=PARAMS, tick_size=0.01,
    )
    s = result.signal
    assert s is not None, result.reason
    assert s.direction == "SELL" and s.htf_bias == "Bearish"
    assert s.entry == 2 * pivot - EXPECTED_BUY["entry"]
    assert s.stop_loss == 2 * pivot - EXPECTED_BUY["stop_loss"]
    assert [t.price for t in s.targets] == [2 * pivot - p for p in EXPECTED_BUY["targets"]]
    assert (s.sweep.side, s.sweep.direction) == ("high", "bearish")


def test_no_setup_when_entry_already_traded():
    ltf = ltf_bullish()
    ltf.loc[len(ltf) - 1, "low"] = 2646.0  # latest bar dips into the entry
    result = bullish(ltf=ltf)
    assert result.signal is None
    assert "already traded back to entry" in result.reason


def test_no_setup_when_ask_at_entry():
    result = bullish(bid=2646.2, ask=2646.5)
    assert result.signal is None and "buy limit not possible" in result.reason


def test_no_setup_when_first_target_already_hit():
    ltf = ltf_bullish()
    ltf.loc[len(ltf) - 1, "high"] = 2673.0
    result = bullish(ltf=ltf)
    assert result.signal is None and "already reached" in result.reason


def test_no_setup_when_stop_too_wide():
    result = bullish(replace(PARAMS, max_sl_distance=5.0))
    assert result.signal is None and "exceeds max" in result.reason


def test_no_setup_without_sweep():
    ltf = ltf_bullish()
    ltf.loc[29, "low"] = 2640.5  # the London wick no longer takes the Asia low
    result = bullish(ltf=ltf)
    assert result.signal is None and "sweep" in result.reason


def test_no_setup_when_sweep_is_stale():
    result = bullish(replace(PARAMS, max_bars_since_sweep=5))
    assert result.signal is None and "sweep" in result.reason


def test_no_setup_with_neutral_htf():
    flat = make_bars([(2650, 2651, 2649, 2650)] * 30, freq="4h")
    result = bullish(htf=flat)
    assert result.signal is None and "neutral" in result.reason


def test_to_tick_rounding_directions():
    assert _to_tick(2646.505, 0.01, "down") == 2646.50
    assert _to_tick(2646.501, 0.01, "up") == 2646.51
    assert _to_tick(2646.50, 0.01, "up") == 2646.50  # already on a tick
    assert _to_tick(2646.505, None, "down") == 2646.505


# ---------------------------------------------------------------- pipeline


@pytest.fixture
def fake():
    fake = FakeMT5()
    fake.rates = {
        FakeMT5.TIMEFRAME_H4: to_rates(htf_bullish()),
        FakeMT5.TIMEFRAME_M15: to_rates(ltf_bullish()),
    }
    fake.tick = SimpleNamespace(time=1_790_000_000, bid=BULLISH_BID, ask=BULLISH_ASK, last=0.0)
    return fake


def make_pipeline(fake, tmp_path, execute=True, params=PARAMS):
    connector = MT5Connector(trading=TradingSettings(magic_number=MAGIC), mt5_module=fake, retry_delay=0)
    connector.connect()
    return SignalPipeline(
        connector, MT5Executor(connector), AurumReportGenerator(output_dir=tmp_path), params, execute=execute,
    )


def test_pipeline_reports_and_places_order_with_mt5_lot_size(fake, tmp_path):
    fake.send_results = [{"retcode": 10008, "order": 9001}]

    result = make_pipeline(fake, tmp_path).run("XAUUSD")

    assert result.status == "executed", result.message
    # 1% of 10,000 = 100 USD over a 10.00 stop at 1 USD per 0.01 tick -> 0.10 lots
    assert result.lot_size == 0.10

    [request] = fake.sent
    assert request["type"] == FakeMT5.ORDER_TYPE_BUY_LIMIT
    assert request["action"] == FakeMT5.TRADE_ACTION_PENDING
    assert (request["price"], request["sl"], request["tp"]) == (2646.50, 2636.50, 2672.0)
    assert request["volume"] == result.lot_size
    assert request["magic"] == MAGIC
    assert result.order.order == 9001

    html = result.report_path.read_text(encoding="utf-8")
    assert result.report_path.parent == tmp_path
    assert '<p class="card-value" id="lot-headline">0.10</p>' in html
    for text in ("XAUUSD", "2,646.50", "2,636.50", "2,672.00", "2,691.00",
                 "H4 swing high", "Asia low 2640.00 swept to 2637.00", "CHoCH above 2650.00"):
        assert text in html


def test_pipeline_dry_run_reports_without_ordering(fake, tmp_path):
    result = make_pipeline(fake, tmp_path, execute=False).run("XAUUSD")
    assert result.status == "reported"
    assert result.report_path.exists()
    assert fake.sent == []


def test_pipeline_does_not_repeat_the_same_setup(fake, tmp_path):
    pipeline = make_pipeline(fake, tmp_path, execute=False)
    pipeline.run("XAUUSD")
    second = pipeline.run("XAUUSD")
    assert second.status == "skipped" and "already processed" in second.message
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_pipeline_skips_when_position_already_open(fake, tmp_path):
    fake.positions = [SimpleNamespace(symbol="XAUUSD", magic=MAGIC, ticket=1)]
    result = make_pipeline(fake, tmp_path).run("XAUUSD")
    assert result.status == "skipped"
    assert fake.sent == [] and list(tmp_path.glob("*.html")) == []


def test_pipeline_without_setup(fake, tmp_path):
    flat = make_bars([(2650, 2651, 2649, 2650)] * 60)
    fake.rates[FakeMT5.TIMEFRAME_M15] = to_rates(flat)
    result = make_pipeline(fake, tmp_path).run("XAUUSD")
    assert result.status == "no_setup"
    assert fake.sent == []


def test_pipeline_reports_broker_rejection(fake, tmp_path):
    fake.send_results = [{"retcode": 10018}]  # market closed
    result = make_pipeline(fake, tmp_path).run("XAUUSD")
    assert result.status == "order_failed"
    assert "MARKET_CLOSED" in result.message
    assert result.report_path.exists()  # the report is still produced


def test_pipeline_sets_expiration_from_server_time(fake, tmp_path):
    fake.send_results = [{"retcode": 10008}]
    make_pipeline(fake, tmp_path, params=replace(PARAMS, order_expiry_minutes=120)).run("XAUUSD")
    assert fake.sent[0]["expiration"] == 1_790_000_000 + 120 * 60


def test_pipeline_estimates_server_offset_from_tick(fake, tmp_path):
    now = pd.Timestamp.now(tz="UTC")
    fake.tick.time = int((now + pd.Timedelta(hours=3)).timestamp())
    pipeline = make_pipeline(fake, tmp_path, params=replace(PARAMS, server_utc_offset_hours=None))
    assert pipeline._utc_offset(pipeline.connector.get_tick()) == 3
