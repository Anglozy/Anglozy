"""TradingView chart screenshots for Aurum reports, captured with Playwright.

Loads TradingView's lightweight embed widget in headless Chromium, waits for the
chart canvas (``.chart-markup-table``) to render plus a short delay for candles
and indicators, and saves a PNG under ``reports/images/``.

    async with ChartCapturer() as capturer:
        h4 = await capturer.capture_chart("XAUUSD", "H4", "xauusd_h4.png")
        m15 = await capturer.capture_chart("XAUUSD", "M15", "xauusd_m15.png")

``capture_chart`` also works without ``async with``; it then launches and closes
a browser for that one capture. Requires ``pip install playwright`` and a
Chromium build (``playwright install chromium``, or point ``executable_path`` /
``AURUM_CHROMIUM_PATH`` at an existing one).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGES_DIR = PROJECT_ROOT / "reports" / "images"

WIDGET_URL = "https://s.tradingview.com/widgetembed/?symbol={symbol}&interval={interval}&theme={theme}"
CHART_SELECTOR = ".chart-markup-table"
VIEWPORT = {"width": 1280, "height": 720}

# MT5 timeframe names -> TradingView interval codes
TIMEFRAME_CODES = {
    "M1": "1", "M3": "3", "M5": "5", "M15": "15", "M30": "30",
    "H1": "60", "H2": "120", "H3": "180", "H4": "240",
    "D1": "D", "W1": "W", "MN1": "M",
}


class ChartCaptureError(Exception):
    """The chart could not be loaded or saved."""


def timeframe_code(timeframe: str) -> str:
    """TradingView interval code for an MT5 timeframe name (e.g. ``H4`` -> ``240``)."""
    try:
        return TIMEFRAME_CODES[timeframe.upper()]
    except KeyError:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}; use one of {', '.join(TIMEFRAME_CODES)}"
        ) from None


def tradingview_symbol(symbol: str, exchange: str) -> str:
    """``EXCHANGE:SYMBOL`` for TradingView.

    Broker suffixes are stripped (``XAUUSD.m`` / ``XAUUSD-ECN`` -> ``XAUUSD``).
    A symbol that already contains ``:`` is passed through unchanged.
    """
    if ":" in symbol:
        return symbol
    base = re.split(r"[.\-_#]", symbol.strip(), maxsplit=1)[0].upper()
    if not base:
        raise ValueError(f"Invalid symbol {symbol!r}")
    return f"{exchange}:{base}"


class ChartCapturer:
    """Capture TradingView embed charts as PNG screenshots."""

    def __init__(
        self,
        images_dir: str | Path = DEFAULT_IMAGES_DIR,
        base_dir: str | Path = PROJECT_ROOT,
        exchange: str = "OANDA",
        theme: str = "dark",
        render_delay_ms: int = 3000,
        timeout_ms: int = 30_000,
        executable_path: str | None = None,
        url_template: str = WIDGET_URL,
    ) -> None:
        self.images_dir = Path(images_dir)
        self.base_dir = Path(base_dir)
        self.exchange = exchange
        self.theme = theme
        self.render_delay_ms = render_delay_ms
        self.timeout_ms = timeout_ms
        self.executable_path = executable_path or os.getenv("AURUM_CHROMIUM_PATH") or None
        self.url_template = url_template
        self._playwright: Any = None
        self._browser: Any = None

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> ChartCapturer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._browser is None:
            self._browser = await self._launch()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _launch(self) -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ChartCaptureError("Playwright is not installed: pip install playwright") from exc
        self._playwright = await async_playwright().start()
        kwargs: dict[str, Any] = {"headless": True}
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        try:
            return await self._playwright.chromium.launch(**kwargs)
        except Exception as exc:
            await self._playwright.stop()
            self._playwright = None
            raise ChartCaptureError(
                f"Could not launch Chromium ({exc}). Run 'playwright install chromium' "
                "or set AURUM_CHROMIUM_PATH."
            ) from exc

    # ------------------------------------------------------------ capture
    def chart_url(self, symbol: str, timeframe: str) -> str:
        return self.url_template.format(
            symbol=quote(tradingview_symbol(symbol, self.exchange), safe=":"),
            interval=timeframe_code(timeframe),
            theme=self.theme,
        )

    async def capture_chart(self, symbol: str, timeframe: str, output_path: str) -> str:
        """Screenshot ``symbol`` on ``timeframe`` and return the saved path.

        ``output_path`` is a file name (or path) inside ``images_dir``; an
        absolute path is used as given. The returned path is relative to
        ``base_dir`` (the project root by default), e.g.
        ``reports/images/xauusd_h4.png``.
        """
        url = self.chart_url(symbol, timeframe)
        target = Path(output_path)
        if not target.is_absolute():
            target = self.images_dir / target
        if target.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            target = target.with_suffix(".png")
        target.parent.mkdir(parents=True, exist_ok=True)

        owns_browser = self._browser is None
        if owns_browser:
            await self.start()
        page = None
        try:
            page = await self._browser.new_page(viewport=dict(VIEWPORT))
            logger.info("Capturing %s %s chart from %s", symbol, timeframe, url)
            await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            await page.wait_for_selector(CHART_SELECTOR, state="visible", timeout=self.timeout_ms)
            await page.wait_for_timeout(self.render_delay_ms)  # candles and indicators
            await page.screenshot(path=str(target), full_page=False)
        except ChartCaptureError:
            raise
        except Exception as exc:
            raise ChartCaptureError(f"Could not capture {symbol} {timeframe} chart: {exc}") from exc
        finally:
            if page is not None:
                await page.close()
            if owns_browser:
                await self.close()

        logger.info("Saved %s %s chart to %s", symbol, timeframe, target)
        return _relative(target, self.base_dir)


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)
