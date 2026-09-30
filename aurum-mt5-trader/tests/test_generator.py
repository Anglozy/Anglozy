from dataclasses import replace
from datetime import datetime, timezone

import pytest

from aurum.generator import (
    AurumReportGenerator,
    ChartImage,
    TakeProfit,
    build_sample_report,
    calculate_lot_size,
)

FIXED_TIME = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def generator(tmp_path):
    return AurumReportGenerator(output_dir=tmp_path)


def test_generate_writes_html_report(generator, tmp_path):
    path = generator.generate(build_sample_report(), generated_at=FIXED_TIME)

    assert path == tmp_path / "aurum_XAUUSD_BUY_20260930_120000.html"
    html = path.read_text(encoding="utf-8")
    assert "<title>Aurum Report · XAUUSD BUY</title>" in html
    assert "#D4AF37" in html  # stylesheet inlined


def test_render_includes_all_sections(generator):
    html = generator.render(build_sample_report(), FIXED_TIME)

    for text in (
        "Multi-Timeframe Analysis",
        "H4",
        "M15",
        "2,646.80 – 2,650.10",  # FVG range
        "H1 Bullish Order Block",
        "2,641.20",  # stop loss
        "TP3 · HTF liquidity",
        "1 : 2.00",  # TP1 R:R = 14.60 / 7.30
        "0.13",  # 100 USD / (7.30 * 100) = 0.137 -> 0.13
        "Chart Evidence",
        'class="chart-placeholder"',
    ):
        assert text in html


def test_local_chart_image_is_embedded(generator, tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    report = build_sample_report()
    report.charts = [ChartImage("M15 Entry", path=image)]

    html = generator.render(report, FIXED_TIME)

    assert 'src="data:image/png;base64,' in html
    assert 'class="chart-placeholder"' not in html


def test_user_text_is_html_escaped(generator):
    report = build_sample_report()
    report.htf.notes = "<script>alert(1)</script>"

    html = generator.render(report, FIXED_TIME)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_buy_with_stop_above_entry_is_rejected(generator):
    report = build_sample_report()
    report.setup = replace(report.setup, stop_loss=2650.00)

    with pytest.raises(ValueError, match="stop loss must be below entry"):
        generator.render(report)


def test_sell_take_profit_above_entry_is_rejected(generator):
    report = build_sample_report()
    report.setup = replace(
        report.setup,
        direction="SELL",
        stop_loss=2655.00,
        take_profits=[TakeProfit("TP1", 2660.00)],
    )

    with pytest.raises(ValueError, match="take profit 'TP1' must be below entry"):
        generator.render(report)


@pytest.mark.parametrize(
    ("risk_amount", "sl_distance", "expected"),
    [
        (100, 7.30, 0.13),
        (100, 10.0, 0.10),
        (500, 5.0, 1.0),
        (1, 10.0, 0.0),  # below min step
        (1_000_000, 1.0, 100.0),  # capped at max_lot
    ],
)
def test_calculate_lot_size(risk_amount, sl_distance, expected):
    assert calculate_lot_size(risk_amount, sl_distance, contract_size=100) == pytest.approx(expected)
