import pandas as pd
import pytest

from signals.fvg import detect_fvgs
from signals.liquidity import detect_session_sweeps, session_ranges
from signals.order_block import detect_order_blocks
from signals.structure import detect_structure_breaks, find_swings, market_bias, unbroken_swings
from tests.scenarios import bars_from_closes, make_bars, mirror

# ---------------------------------------------------------------- Fair Value Gaps


def test_bullish_fvg():
    bars = make_bars([
        (10.0, 11.0, 9.0, 10.5),
        (10.5, 14.0, 10.4, 13.8),  # displacement
        (13.8, 15.0, 12.0, 14.5),  # low 12 > first high 11
        (14.5, 15.0, 13.0, 14.0),
    ])
    [gap] = detect_fvgs(bars)
    assert (gap.direction, gap.index, gap.low, gap.high) == ("bullish", 1, 11.0, 12.0)
    assert gap.midpoint == 11.5 and gap.size == 1.0
    assert not gap.filled
    assert gap.time == bars["time"][1]


def test_bullish_fvg_filled_when_price_trades_through():
    bars = make_bars([
        (10.0, 11.0, 9.0, 10.5),
        (10.5, 14.0, 10.4, 13.8),
        (13.8, 15.0, 12.0, 14.5),
        (14.5, 15.0, 10.8, 11.0),  # low 10.8 below gap low 11
    ])
    assert detect_fvgs(bars)[0].filled


def test_bearish_fvg():
    bars = make_bars([
        (20.0, 21.0, 19.0, 19.5),
        (19.5, 19.6, 15.0, 15.2),
        (15.2, 18.0, 14.0, 14.5),  # high 18 < first low 19
    ])
    [gap] = detect_fvgs(bars)
    assert (gap.direction, gap.low, gap.high, gap.filled) == ("bearish", 18.0, 19.0, False)


def test_fvg_min_size_and_overlap():
    overlapping = make_bars([(10, 12, 9, 11), (11, 13, 10.5, 12.5), (12.5, 13, 11.5, 12)])
    assert detect_fvgs(overlapping) == []

    small_gap = make_bars([(10, 11, 9, 10.5), (10.5, 12, 10.4, 11.9), (11.9, 12, 11.2, 11.8)])
    assert len(detect_fvgs(small_gap)) == 1
    assert detect_fvgs(small_gap, min_size=0.5) == []


def test_missing_columns_rejected():
    with pytest.raises(ValueError, match="missing columns"):
        detect_fvgs(pd.DataFrame({"time": [], "open": []}))


# ---------------------------------------------------------------- structure
# Closes rise to 13, dip to 10, break above 13 (CHoCH), pull back, break higher (BOS).
TREND = [10, 11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 15, 14, 13, 14, 15, 16, 17]


def test_find_swings():
    swings = find_swings(bars_from_closes(TREND), lookback=2)
    assert [(s.kind, s.index, s.price) for s in swings] == [
        ("high", 3, 13.5), ("low", 6, 9.5), ("high", 11, 15.5), ("low", 13, 12.5),
    ]


def test_structure_breaks_choch_then_bos():
    breaks = detect_structure_breaks(bars_from_closes(TREND), lookback=2)
    assert [(b.direction, b.kind, b.index, b.level, b.swing_index) for b in breaks] == [
        ("bullish", "CHoCH", 10, 13.5, 3),
        ("bullish", "BOS", 16, 15.5, 11),
    ]


def test_bearish_structure_is_mirrored():
    breaks = detect_structure_breaks(mirror(bars_from_closes(TREND), pivot=10), lookback=2)
    assert [(b.direction, b.kind) for b in breaks] == [("bearish", "CHoCH"), ("bearish", "BOS")]


def test_market_bias():
    bars = bars_from_closes(TREND)
    assert market_bias(bars, lookback=2) == "Bullish"
    assert market_bias(mirror(bars, pivot=10), lookback=2) == "Bearish"
    assert market_bias(bars_from_closes([10.0] * 20), lookback=2) == "Neutral"


def test_unbroken_swings():
    bars = bars_from_closes(TREND[:14])  # stop after the pullback: high 15.5 not yet taken
    assert [s.price for s in unbroken_swings(bars, 2, "high")] == [15.5]


# ---------------------------------------------------------------- order blocks


def test_bullish_order_block_is_last_down_candle_before_break():
    bars = bars_from_closes(TREND)
    blocks = detect_order_blocks(bars, lookback=2)

    first = blocks[0]
    assert (first.direction, first.index, first.break_index) == ("bullish", 6, 10)
    assert (first.low, first.high) == (9.5, 11.5)  # candle 6: open 11, close 10
    assert not first.mitigated


def test_order_block_mitigation():
    closes = TREND[:12] + [14, 13, 12, 11]  # after the CHoCH, price returns into the block
    blocks = detect_order_blocks(bars_from_closes(closes), lookback=2)
    assert blocks[0].mitigated


def test_bearish_order_block():
    blocks = detect_order_blocks(mirror(bars_from_closes(TREND), pivot=10), lookback=2)
    first = blocks[0]
    assert first.direction == "bearish"
    assert (first.low, first.high) == (8.5, 10.5)  # mirror of 9.5-11.5 around 10


# ---------------------------------------------------------------- session liquidity


def asia_then(rows_after, start="2026-09-29 00:00"):
    """24 Asia bars (00:00-05:45 UTC) ranging 100-110, then ``rows_after``."""
    rows = [(105, 106, 104, 105)] * 24
    rows[5] = (105, 110, 104, 106)
    rows[15] = (105, 106, 100, 104)
    return make_bars(rows + rows_after, start=start)


def test_session_ranges_only_include_completed_sessions():
    bars = asia_then([(105, 106, 104, 105)] * 8)  # 06:00-07:45; London still open
    [asia] = session_ranges(bars)
    assert (asia.name, asia.start_index, asia.end_index, asia.high, asia.low) == ("Asia", 0, 23, 110, 100)


def test_asia_low_sweep_is_bullish():
    bars = asia_then([
        (105, 106, 104, 105),
        (105, 105, 98.5, 101),   # wick below 100, close back inside
        (101, 104, 100.5, 103),
    ])
    [sweep] = detect_session_sweeps(bars)
    assert (sweep.session, sweep.side, sweep.direction) == ("Asia", "low", "bullish")
    assert (sweep.index, sweep.level, sweep.extreme) == (25, 100, 98.5)


def test_asia_high_sweep_is_bearish():
    bars = asia_then([(105, 111.2, 104, 108)])
    [sweep] = detect_session_sweeps(bars)
    assert (sweep.side, sweep.direction, sweep.extreme) == ("high", "bearish", 111.2)


def test_breakout_is_not_a_sweep():
    bars = asia_then([
        (105, 105, 97, 98),      # closes below the low: breakout
        (98, 101, 96, 100.5),    # later wick-and-reclaim no longer counts
    ])
    assert detect_session_sweeps(bars) == []


def test_sweep_window_limits_how_late_a_sweep_counts():
    bars = asia_then([(105, 106, 104, 105)] * 10 + [(105, 105, 98, 101)])
    assert detect_session_sweeps(bars, max_bars_after=5) == []
    assert len(detect_session_sweeps(bars, max_bars_after=20)) == 1


def test_london_session_sweep():
    london = [(105, 106, 104, 105)] * 4 + [(105, 107, 103, 105)] * 20  # 06:00-11:45
    after = [(105, 107.5, 104, 106)]  # 12:00 wick above London high 107
    bars = asia_then(london + after)
    sweeps = detect_session_sweeps(bars)
    assert [(s.session, s.side) for s in sweeps] == [("London", "high")]


def test_server_time_offset_shifts_sessions():
    # Same data stamped in UTC+3 server time: sessions only line up with the offset.
    bars = asia_then([(105, 105, 98.5, 101)], start="2026-09-29 03:00")
    assert detect_session_sweeps(bars, utc_offset_hours=3)[0].session == "Asia"
    assert detect_session_sweeps(bars, utc_offset_hours=0) == []
