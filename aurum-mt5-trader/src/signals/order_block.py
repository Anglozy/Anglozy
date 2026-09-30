"""Order Block detection: the last opposing candle before a market structure break."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .bars import Direction, ohlc
from .structure import StructureBreak, detect_structure_breaks


@dataclass(frozen=True)
class OrderBlock:
    direction: Direction
    index: int  # the order block candle
    time: pd.Timestamp
    low: float
    high: float
    break_index: int  # candle that broke structure
    mitigated: bool  # price has traded back into the block since the break


def detect_order_blocks(
    bars: pd.DataFrame,
    lookback: int = 2,
    breaks: list[StructureBreak] | None = None,
) -> list[OrderBlock]:
    """One order block per structure break, oldest first.

    Bullish: find the lowest point of the leg between the broken swing high and
    the breaking candle, then take the last bearish candle at or before it. That
    candle's full range is the block. Bearish is mirrored.
    """
    data = ohlc(bars)
    o, h, l, c = data.open, data.high, data.low, data.close
    breaks = detect_structure_breaks(bars, lookback) if breaks is None else breaks
    blocks: list[OrderBlock] = []

    for brk in breaks:
        start, end = brk.swing_index, brk.index
        if brk.direction == "bullish":
            extreme = start + int(l[start:end + 1].argmin())
            is_opposing = lambda k: c[k] < o[k]  # noqa: E731
        else:
            extreme = start + int(h[start:end + 1].argmax())
            is_opposing = lambda k: c[k] > o[k]  # noqa: E731

        candle = next((k for k in range(extreme, start, -1) if is_opposing(k)), extreme)
        ob_low, ob_high = float(l[candle]), float(h[candle])
        after = slice(end + 1, None)
        if brk.direction == "bullish":
            mitigated = bool((l[after] <= ob_high).any())
        else:
            mitigated = bool((h[after] >= ob_low).any())

        blocks.append(OrderBlock(
            brk.direction, candle, data.time[candle], ob_low, ob_high, brk.index, mitigated,
        ))
    return blocks
