"""Aurum HTML report generator.

Renders structured trade data into a self-contained, styled HTML report using the
Jinja2 template in ``templates/aurum_template.html`` and saves it to ``reports/``.

This module only renders data it is given: it never fetches market data or places
orders. Run it directly to generate a sample XAUUSD report:

    python src/aurum/generator.py
"""

from __future__ import annotations

import base64
import logging
import math
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE_DIR = PROJECT_ROOT / "templates"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports"
DEFAULT_STYLESHEET = Path(__file__).resolve().parent / "styles" / "aurum.css"
TEMPLATE_NAME = "aurum_template.html"

VALID_BIASES = {"Bullish", "Bearish", "Neutral"}
VALID_DIRECTIONS = {"BUY", "SELL"}


# --------------------------------------------------------------------------- #
# Input data structures
# --------------------------------------------------------------------------- #
@dataclass
class HTFAnalysis:
    """Higher-timeframe context that sets the directional bias."""

    timeframe: str
    bias: str
    structure: str
    key_level: str
    draw_on_liquidity: str
    notes: str = ""


@dataclass
class LTFAnalysis:
    """Lower-timeframe execution trigger."""

    timeframe: str
    bias: str
    trigger: str
    confirmation: str
    liquidity_sweep: str
    notes: str = ""


@dataclass
class PriceZone:
    """A price range such as an Order Block."""

    label: str
    low: float
    high: float


@dataclass
class TakeProfit:
    label: str
    price: float
    close_percent: int | None = None


@dataclass
class TradeSetup:
    pair: str
    direction: str
    entry: float
    stop_loss: float
    fvg_low: float
    fvg_high: float
    take_profits: list[TakeProfit]
    order_blocks: list[PriceZone] = field(default_factory=list)
    order_type: str = "Limit order"
    invalidation: str = ""


@dataclass
class RiskProfile:
    """Account risk inputs.

    ``contract_size`` is the account-currency value of a 1.0 price move for one lot
    (100 for XAUUSD on most brokers: 100 oz per lot). If ``lot_size`` is omitted it is
    derived from balance, risk % and stop distance, rounded down to ``lot_step``.
    """

    account_balance: float
    risk_percent: float
    lot_size: float | None = None
    contract_size: float = 100.0
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 100.0
    currency: str = "USD"


@dataclass
class ChartImage:
    """A chart screenshot. ``path`` may be a local file, an http(s) URL or ``None``
    (renders a placeholder)."""

    title: str
    path: str | Path | None = None
    caption: str = ""


@dataclass
class TradeReport:
    setup: TradeSetup
    htf: HTFAnalysis
    ltf: LTFAnalysis
    risk: RiskProfile
    charts: list[ChartImage] = field(default_factory=list)
    strategy_name: str = "SMC · FVG + Order Block"
    digits: int = 2


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #
class AurumReportGenerator:
    """Render :class:`TradeReport` objects to styled HTML files."""

    def __init__(
        self,
        template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
        output_dir: str | Path = DEFAULT_OUTPUT_DIR,
        stylesheet: str | Path = DEFAULT_STYLESHEET,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.stylesheet = Path(stylesheet)
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["price"] = _format_price

    def render(self, report: TradeReport, generated_at: datetime | None = None) -> str:
        """Validate ``report`` and return the rendered HTML as a string."""
        _validate(report)
        generated_at = generated_at or datetime.now(timezone.utc)
        context = self._build_context(report, generated_at)
        return self.env.get_template(TEMPLATE_NAME).render(**context)

    def generate(
        self,
        report: TradeReport,
        filename: str | None = None,
        generated_at: datetime | None = None,
    ) -> Path:
        """Render ``report`` and write it to the output directory. Returns the file path."""
        generated_at = generated_at or datetime.now(timezone.utc)
        html = self.render(report, generated_at)
        if filename is None:
            stamp = generated_at.strftime("%Y%m%d_%H%M%S")
            filename = f"aurum_{report.setup.pair}_{report.setup.direction}_{stamp}.html"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / filename
        path.write_text(html, encoding="utf-8")
        logger.info("Aurum report written to %s", path)
        return path

    # ----------------------------------------------------------------- helpers
    def _build_context(self, report: TradeReport, generated_at: datetime) -> dict:
        setup, risk = report.setup, report.risk
        sl_distance = abs(setup.entry - setup.stop_loss)

        take_profits = []
        for tp in setup.take_profits:
            distance = abs(tp.price - setup.entry)
            take_profits.append(
                {
                    "label": tp.label,
                    "price": tp.price,
                    "close_percent": tp.close_percent,
                    "distance": distance,
                    "rr": distance / sl_distance,
                }
            )

        risk_amount = risk.account_balance * risk.risk_percent / 100
        lot_size = risk.lot_size
        if lot_size is None:
            lot_size = calculate_lot_size(
                risk_amount, sl_distance, risk.contract_size, risk.lot_step, risk.max_lot
            )

        return {
            "styles": self.stylesheet.read_text(encoding="utf-8"),
            "strategy_name": report.strategy_name,
            "generated_at": generated_at.strftime("%Y-%m-%d %H:%M UTC"),
            "report_id": f"{setup.pair}-{generated_at.strftime('%Y%m%d%H%M%S')}",
            "digits": report.digits,
            "htf": report.htf,
            "ltf": report.ltf,
            "setup": {
                "pair": setup.pair,
                "direction": setup.direction,
                "order_type": setup.order_type,
                "entry": setup.entry,
                "stop_loss": setup.stop_loss,
                "fvg_low": setup.fvg_low,
                "fvg_high": setup.fvg_high,
                "order_blocks": setup.order_blocks,
                "take_profits": take_profits,
                "invalidation": setup.invalidation,
            },
            "risk": {
                "account_balance": risk.account_balance,
                "risk_percent": risk.risk_percent,
                "risk_amount": risk_amount,
                "sl_distance": sl_distance,
                "primary_rr": take_profits[0]["rr"],
                "lot_size": lot_size,
                "contract_size": risk.contract_size,
                "lot_step": risk.lot_step,
                "min_lot": risk.min_lot,
                "currency": risk.currency,
            },
            "calc_config": {
                "sl_distance": sl_distance,
                "contract_size": risk.contract_size,
                "lot_step": risk.lot_step,
                "min_lot": risk.min_lot,
                "max_lot": risk.max_lot,
                "currency": risk.currency,
            },
            "charts": [
                {"title": c.title, "caption": c.caption, "src": _image_src(c.path)}
                for c in report.charts
            ],
        }


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def calculate_lot_size(
    risk_amount: float,
    sl_distance: float,
    contract_size: float,
    lot_step: float = 0.01,
    max_lot: float = 100.0,
) -> float:
    """Lots such that hitting the stop loses at most ``risk_amount``, rounded down."""
    if sl_distance <= 0 or contract_size <= 0:
        raise ValueError("sl_distance and contract_size must be positive")
    raw = risk_amount / (sl_distance * contract_size)
    # Small epsilon guards against float error flooring e.g. 0.5 / 0.01 to 49.
    steps = math.floor(raw / lot_step + 1e-9)
    return round(min(steps * lot_step, max_lot), 8)


def _format_price(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def _image_src(path: str | Path | None) -> str | None:
    """Return a src usable in <img>: data URI for local files, URLs passed through."""
    if path is None:
        return None
    text = str(path)
    if text.startswith(("http://", "https://", "data:")):
        return text
    file = Path(path)
    if not file.is_file():
        logger.warning("Chart image not found, using placeholder: %s", file)
        return None
    mime = mimetypes.guess_type(file.name)[0] or "image/png"
    encoded = base64.b64encode(file.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _validate(report: TradeReport) -> None:
    s = report.setup
    if s.direction not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(VALID_DIRECTIONS)}, got {s.direction!r}")
    for tf in (report.htf, report.ltf):
        if tf.bias not in VALID_BIASES:
            raise ValueError(f"bias must be one of {sorted(VALID_BIASES)}, got {tf.bias!r}")
    if not s.take_profits:
        raise ValueError("at least one take profit is required")
    if s.fvg_low > s.fvg_high:
        raise ValueError("fvg_low must be <= fvg_high")
    for ob in s.order_blocks:
        if ob.low > ob.high:
            raise ValueError(f"order block {ob.label!r}: low must be <= high")

    is_buy = s.direction == "BUY"
    if (s.stop_loss >= s.entry) if is_buy else (s.stop_loss <= s.entry):
        side = "below" if is_buy else "above"
        raise ValueError(f"{s.direction} stop loss must be {side} entry")
    for tp in s.take_profits:
        if (tp.price <= s.entry) if is_buy else (tp.price >= s.entry):
            side = "above" if is_buy else "below"
            raise ValueError(f"{s.direction} take profit {tp.label!r} must be {side} entry")

    r = report.risk
    if r.account_balance <= 0 or not 0 < r.risk_percent <= 100:
        raise ValueError("account_balance must be > 0 and risk_percent in (0, 100]")


# --------------------------------------------------------------------------- #
# Sample execution
# --------------------------------------------------------------------------- #
def build_sample_report() -> TradeReport:
    """Dummy XAUUSD long setup used for demos and smoke tests."""
    return TradeReport(
        setup=TradeSetup(
            pair="XAUUSD",
            direction="BUY",
            order_type="Buy limit",
            entry=2648.50,
            stop_loss=2641.20,
            fvg_low=2646.80,
            fvg_high=2650.10,
            order_blocks=[
                PriceZone("H1 Bullish Order Block", 2642.00, 2645.30),
                PriceZone("H4 Bullish Order Block", 2631.40, 2636.90),
            ],
            take_profits=[
                TakeProfit("TP1 · Internal high", 2663.10, close_percent=50),
                TakeProfit("TP2 · Asian high", 2672.40, close_percent=30),
                TakeProfit("TP3 · HTF liquidity", 2688.00, close_percent=20),
            ],
            invalidation="M15 close below 2641.20 (H1 order block low).",
        ),
        htf=HTFAnalysis(
            timeframe="H4",
            bias="Bullish",
            structure="Higher highs / higher lows, BOS at 2661.80",
            key_level="H4 OB 2631.40 – 2636.90",
            draw_on_liquidity="Buy-side liquidity above 2688.00",
            notes="Price pulled back into discount after breaking structure; "
            "H4 demand remains unmitigated.",
        ),
        ltf=LTFAnalysis(
            timeframe="M15",
            bias="Bullish",
            trigger="Retrace into M15 FVG 2646.80 – 2650.10",
            confirmation="CHoCH above 2652.40",
            liquidity_sweep="Sell-side swept below 2643.90 (London open)",
            notes="Enter on the first touch of the FVG after the displacement leg.",
        ),
        risk=RiskProfile(account_balance=10_000, risk_percent=1.0),
        charts=[
            ChartImage("H4 Bias", caption="Structure, BOS and H4 order block"),
            ChartImage("M15 Entry", caption="Liquidity sweep, CHoCH and FVG entry"),
        ],
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    output = AurumReportGenerator().generate(build_sample_report())
    print(f"Sample XAUUSD report saved to: {output}")
