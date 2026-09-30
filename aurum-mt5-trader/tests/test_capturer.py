import asyncio
import base64
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from aurum import generator as generator_module
from aurum.capturer import (
    CHART_SELECTOR,
    ChartCaptureError,
    ChartCapturer,
    timeframe_code,
    tradingview_symbol,
)
from aurum.generator import AurumReportGenerator, ChartImage, build_sample_report
from config.settings import TradingSettings
from config.strategy import StrategyParameters
from mt5.connector import MT5Connector
from mt5.executor import MT5Executor
from signals.parser import SignalPipeline
from tests.fake_mt5 import FakeMT5
from tests.scenarios import BULLISH_ASK, BULLISH_BID, htf_bullish, ltf_bullish, to_rates

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def fake_png(label: str) -> bytes:
    return PNG_HEADER + label.encode()


# --------------------------------------------------------------------------- #
# Symbols, timeframes, URLs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("tf", "code"), [("M15", "15"), ("h4", "240"), ("H1", "60"), ("D1", "D"), ("W1", "W")])
def test_timeframe_code(tf, code):
    assert timeframe_code(tf) == code


def test_unknown_timeframe():
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        timeframe_code("M7")


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("XAUUSD", "OANDA:XAUUSD"),
        ("xauusd.m", "OANDA:XAUUSD"),
        ("XAUUSD-ECN", "OANDA:XAUUSD"),
        ("XAUUSD_i", "OANDA:XAUUSD"),
        ("FX:XAUUSD", "FX:XAUUSD"),  # explicit exchange passes through
    ],
)
def test_tradingview_symbol(symbol, expected):
    assert tradingview_symbol(symbol, "OANDA") == expected


def test_chart_url_uses_widget_embed():
    capturer = ChartCapturer(exchange="FX")
    assert capturer.chart_url("XAUUSD", "H4") == (
        "https://s.tradingview.com/widgetembed/?symbol=FX:XAUUSD&interval=240&theme=dark"
    )
    assert "interval=15" in capturer.chart_url("XAUUSD", "M15")


# --------------------------------------------------------------------------- #
# capture_chart with a mocked browser
# --------------------------------------------------------------------------- #
class FakePage:
    def __init__(self, fail_on: str | None = None):
        self.fail_on = fail_on
        self.goto = AsyncMock(side_effect=self._maybe_fail("goto"))
        self.wait_for_selector = AsyncMock(side_effect=self._maybe_fail("wait_for_selector"))
        self.wait_for_timeout = AsyncMock()
        self.close = AsyncMock()

    def _maybe_fail(self, name):
        async def run(*args, **kwargs):
            if self.fail_on == name:
                raise TimeoutError(f"{name} timed out")
        return run

    async def screenshot(self, path, full_page):
        Path(path).write_bytes(fake_png(Path(path).stem))


class FakeBrowser:
    def __init__(self, fail_on=None):
        self.pages: list[FakePage] = []
        self.viewports: list[dict] = []
        self.fail_on = fail_on
        self.closed = False

    async def new_page(self, viewport):
        self.viewports.append(viewport)
        page = FakePage(self.fail_on)
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


def make_capturer(tmp_path, browser=None, **kwargs):
    capturer = ChartCapturer(images_dir=tmp_path / "reports" / "images", base_dir=tmp_path, **kwargs)
    browser = browser or FakeBrowser()
    capturer.launches = 0

    async def launch():
        capturer.launches += 1
        return browser

    capturer._launch = launch
    return capturer, browser


def test_capture_chart_flow(tmp_path):
    capturer, browser = make_capturer(tmp_path, exchange="FX")

    path = asyncio.run(capturer.capture_chart("XAUUSD", "H4", "xauusd_h4.png"))

    assert path == "reports/images/xauusd_h4.png"
    assert (tmp_path / path).read_bytes() == fake_png("xauusd_h4")
    assert browser.viewports == [{"width": 1280, "height": 720}]
    page = browser.pages[0]
    page.goto.assert_awaited_once()
    assert page.goto.await_args.args[0] == (
        "https://s.tradingview.com/widgetembed/?symbol=FX:XAUUSD&interval=240&theme=dark"
    )
    page.wait_for_selector.assert_awaited_once()
    assert page.wait_for_selector.await_args.args == (CHART_SELECTOR,)
    assert page.wait_for_selector.await_args.kwargs["state"] == "visible"
    page.wait_for_timeout.assert_awaited_once_with(3000)
    page.close.assert_awaited_once()
    assert browser.closed  # capture_chart launched the browser, so it closes it


def test_capture_adds_png_suffix(tmp_path):
    capturer, _ = make_capturer(tmp_path)
    path = asyncio.run(capturer.capture_chart("XAUUSD", "M15", "entry"))
    assert path == "reports/images/entry.png"


def test_context_manager_reuses_one_browser(tmp_path):
    capturer, browser = make_capturer(tmp_path)

    async def run():
        async with capturer:
            return await asyncio.gather(
                capturer.capture_chart("XAUUSD", "H4", "h4.png"),
                capturer.capture_chart("XAUUSD", "M15", "m15.png"),
            )

    assert asyncio.run(run()) == ["reports/images/h4.png", "reports/images/m15.png"]
    assert capturer.launches == 1
    assert len(browser.pages) == 2 and browser.closed


@pytest.mark.parametrize("step", ["goto", "wait_for_selector"])
def test_capture_failure_raises_and_cleans_up(tmp_path, step):
    capturer, browser = make_capturer(tmp_path, browser=FakeBrowser(fail_on=step))

    with pytest.raises(ChartCaptureError, match=f"XAUUSD H4 chart: {step} timed out"):
        asyncio.run(capturer.capture_chart("XAUUSD", "H4", "h4.png"))

    browser.pages[0].close.assert_awaited_once()
    assert browser.closed
    assert not (tmp_path / "reports/images/h4.png").exists()


def test_missing_playwright_gives_clear_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    capturer = ChartCapturer(images_dir=tmp_path)
    with pytest.raises(ChartCaptureError, match="pip install playwright"):
        asyncio.run(capturer.capture_chart("XAUUSD", "H4", "h4.png"))


# --------------------------------------------------------------------------- #
# Real Playwright + Chromium against a local stand-in for the widget
# --------------------------------------------------------------------------- #
def chromium_path() -> str | None:
    candidate = Path("/opt/pw-browsers/chromium")
    return str(candidate) if candidate.exists() else None


def local_widget(tmp_path: Path, with_chart: bool = True) -> str:
    """HTML page that renders a .chart-markup-table canvas after a short delay."""
    script = """
      setTimeout(() => {
        const c = document.createElement('canvas');
        c.className = 'chart-markup-table'; c.width = 1200; c.height = 650;
        const g = c.getContext('2d'); g.fillStyle = '#131722'; g.fillRect(0, 0, 1200, 650);
        g.fillStyle = '#D4AF37'; g.font = '32px sans-serif';
        g.fillText(new URLSearchParams(location.search).get('symbol') + ' ' +
                   new URLSearchParams(location.search).get('interval'), 40, 60);
        document.body.appendChild(c);
      }, 300);
    """ if with_chart else ""
    page = tmp_path / "widget.html"
    page.write_text(f"<html><body style='margin:0;background:#000'><script>{script}</script></body></html>")
    return page.as_uri() + "?symbol={symbol}&interval={interval}&theme={theme}"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == PNG_HEADER
    return struct.unpack(">II", data[16:24])


@pytest.fixture
def real_capturer(tmp_path):
    pytest.importorskip("playwright.async_api")

    def build(**kwargs):
        return ChartCapturer(
            images_dir=tmp_path / "images", base_dir=tmp_path,
            executable_path=chromium_path(), render_delay_ms=100, **kwargs,
        )

    async def probe():
        capturer = build()
        await capturer.start()
        await capturer.close()

    try:
        asyncio.run(probe())
    except ChartCaptureError as exc:
        pytest.skip(f"Chromium not available: {exc}")
    return build


def test_real_browser_screenshot(tmp_path, real_capturer):
    capturer = real_capturer(url_template=local_widget(tmp_path))

    path = asyncio.run(capturer.capture_chart("XAUUSD", "H4", "xauusd_h4.png"))

    assert path == "images/xauusd_h4.png"
    assert png_size(tmp_path / path) == (1280, 720)


def test_real_browser_times_out_without_chart(tmp_path, real_capturer):
    capturer = real_capturer(url_template=local_widget(tmp_path, with_chart=False), timeout_ms=1000)
    with pytest.raises(ChartCaptureError, match="chart-markup-table"):
        asyncio.run(capturer.capture_chart("XAUUSD", "H4", "h4.png"))


# --------------------------------------------------------------------------- #
# Generator: screenshots land in the HTF / LTF slots
# --------------------------------------------------------------------------- #
def test_generator_embeds_htf_and_ltf_screenshots(tmp_path):
    htf, ltf = tmp_path / "h4.png", tmp_path / "m15.png"
    htf.write_bytes(fake_png("H4-SHOT"))
    ltf.write_bytes(fake_png("M15-SHOT"))
    report = build_sample_report()
    report.charts = []
    report.htf_chart, report.ltf_chart = htf, ltf

    html = AurumReportGenerator(output_dir=tmp_path).render(report)

    h4_uri = "data:image/png;base64," + base64.b64encode(fake_png("H4-SHOT")).decode()
    m15_uri = "data:image/png;base64," + base64.b64encode(fake_png("M15-SHOT")).decode()
    assert f'<img src="{h4_uri}" alt="H4 Bias">' in html
    assert f'<img src="{m15_uri}" alt="M15 Entry">' in html
    assert html.index(h4_uri) < html.index(m15_uri)
    assert 'class="chart-placeholder"' not in html


def test_generator_keeps_custom_slot_titles(tmp_path):
    shot = tmp_path / "h4.png"
    shot.write_bytes(fake_png("x"))
    report = build_sample_report()  # has custom chart titles/captions
    report.htf_chart = shot

    html = AurumReportGenerator(output_dir=tmp_path).render(report)

    assert 'alt="H4 Bias"' in html and "Structure, BOS and H4 order block" in html
    assert html.count('class="chart-placeholder"') == 1  # LTF slot still empty


def test_generator_resolves_capturer_relative_paths(tmp_path, monkeypatch):
    (tmp_path / "reports" / "images").mkdir(parents=True)
    (tmp_path / "reports/images/h4.png").write_bytes(fake_png("rel"))
    monkeypatch.setattr(generator_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path / "reports")  # not the project root

    report = build_sample_report()
    report.htf_chart = "reports/images/h4.png"
    html = AurumReportGenerator(output_dir=tmp_path).render(report)

    assert "data:image/png;base64," in html


def test_generator_missing_screenshot_falls_back_to_placeholder(tmp_path):
    report = build_sample_report()
    report.charts = [ChartImage("H4 Bias"), ChartImage("M15 Entry")]
    report.htf_chart = tmp_path / "missing.png"
    html = AurumReportGenerator(output_dir=tmp_path).render(report)
    assert html.count('class="chart-placeholder"') == 2


# --------------------------------------------------------------------------- #
# Pipeline wiring
# --------------------------------------------------------------------------- #
class StubCapturer:
    """Async stand-in for ChartCapturer that writes fake PNGs."""

    def __init__(self, images_dir: Path, fail: set[str] = frozenset(), fail_start=False):
        self.images_dir = images_dir
        self.fail = fail
        self.fail_start = fail_start
        self.calls: list[tuple[str, str, str]] = []
        self.entered = self.exited = 0

    async def __aenter__(self):
        if self.fail_start:
            raise ChartCaptureError("Could not launch Chromium")
        self.entered += 1
        return self

    async def __aexit__(self, *exc):
        self.exited += 1

    async def capture_chart(self, symbol, timeframe, output_path):
        self.calls.append((symbol, timeframe, output_path))
        if timeframe in self.fail:
            raise ChartCaptureError(f"{timeframe} widget did not load")
        path = self.images_dir / output_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fake_png(f"{timeframe}-chart"))
        return str(path)


@pytest.fixture
def fake():
    fake = FakeMT5()
    fake.rates = {
        FakeMT5.TIMEFRAME_H4: to_rates(htf_bullish()),
        FakeMT5.TIMEFRAME_M15: to_rates(ltf_bullish()),
    }
    fake.tick = SimpleNamespace(time=1_790_000_000, bid=BULLISH_BID, ask=BULLISH_ASK, last=0.0)
    fake.send_results = [{"retcode": 10008, "order": 1}]
    return fake


def run_pipeline(fake, tmp_path, capturer):
    connector = MT5Connector(trading=TradingSettings(), mt5_module=fake, retry_delay=0)
    connector.connect()
    pipeline = SignalPipeline(
        connector, MT5Executor(connector), AurumReportGenerator(output_dir=tmp_path),
        StrategyParameters(server_utc_offset_hours=0), chart_capturer=capturer,
    )
    return pipeline.run("XAUUSD")


def test_pipeline_captures_h4_and_m15_before_report(fake, tmp_path):
    capturer = StubCapturer(tmp_path / "images")

    result = run_pipeline(fake, tmp_path, capturer)

    assert result.status == "executed"
    assert [(s, tf) for s, tf, _ in capturer.calls] == [("XAUUSD", "H4"), ("XAUUSD", "M15")]
    assert all(name.startswith("xauusd_") and name.endswith(".png") for _, _, name in capturer.calls)
    assert capturer.entered == capturer.exited == 1
    assert all(p and Path(p).exists() for p in result.chart_paths)

    html = result.report_path.read_text(encoding="utf-8")
    assert html.count("data:image/png;base64,") == 2
    assert 'class="chart-placeholder"' not in html


def test_pipeline_uses_placeholder_for_failed_chart(fake, tmp_path):
    result = run_pipeline(fake, tmp_path, StubCapturer(tmp_path / "images", fail={"M15"}))

    assert result.status == "executed"
    assert result.chart_paths[0] is not None and result.chart_paths[1] is None
    html = result.report_path.read_text(encoding="utf-8")
    assert html.count("data:image/png;base64,") == 1
    assert html.count('class="chart-placeholder"') == 1


def test_pipeline_trades_even_if_browser_cannot_start(fake, tmp_path):
    result = run_pipeline(fake, tmp_path, StubCapturer(tmp_path, fail_start=True))

    assert result.status == "executed"
    assert result.chart_paths == (None, None)
    assert result.report_path.read_text(encoding="utf-8").count('class="chart-placeholder"') == 2
    assert len(fake.sent) == 1


def test_pipeline_without_capturer_does_not_capture(fake, tmp_path):
    result = run_pipeline(fake, tmp_path, None)
    assert result.chart_paths == (None, None)


# --------------------------------------------------------------------------- #
# main.py wiring
# --------------------------------------------------------------------------- #
@pytest.fixture
def main_env(monkeypatch, fake):
    for name in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "AURUM_SYMBOL", "AURUM_CHROMIUM_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AURUM_TV_EXCHANGE", "FX")
    monkeypatch.delenv("AURUM_CHARTS_ENABLED", raising=False)
    created = []

    def factory(**kwargs):
        stub = StubCapturer(kwargs["images_dir"])
        stub.kwargs = kwargs
        created.append(stub)
        return stub

    monkeypatch.setattr(main, "ChartCapturer", factory)
    return created


def test_main_wires_chart_capturer(main_env, fake, tmp_path):
    code = main.main(["--utc-offset", "0", "--reports-dir", str(tmp_path)], mt5_module=fake)

    assert code == main.EXIT_OK
    [stub] = main_env
    assert stub.kwargs["images_dir"] == tmp_path / "images"
    assert stub.kwargs["exchange"] == "FX"
    assert [tf for _, tf, _ in stub.calls] == ["H4", "M15"]
    [report] = tmp_path.glob("aurum_*.html")
    assert report.read_text(encoding="utf-8").count("data:image/png;base64,") == 2


def test_main_no_charts_flag(main_env, fake, tmp_path):
    main.main(["--utc-offset", "0", "--reports-dir", str(tmp_path), "--no-charts"], mt5_module=fake)
    assert main_env == []
