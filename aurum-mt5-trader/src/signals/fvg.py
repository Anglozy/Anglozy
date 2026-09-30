"""Fair Value Gap (3-bar imbalance) detection."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .bars import Direction, ohlc


@dataclass(frozen=True)
class FairValueGap:
    direction: Direction
    index: int  # the middle (displacement) candle
    time: pd.Timestamp
    low: float
    high: float
    filled: bool  # a later candle traded through the entire gap

    @property
    def size(self) -> float:
        return self.high - self.low

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2


def detect_fvgs(bars: pd.DataFrame, min_size: float = 0.0) -> list[FairValueGap]:
    """Find every Fair Value Gap in ``bars``, oldest first.

    Bullish: candle 1 high < candle 3 low; the gap is (high1, low3).
    Bearish: candle 1 low > candle 3 high; the gap is (high3, low1).
    Gaps smaller than ``min_size`` are ignored.
    """
    data = ohlc(bars)
    high, low = data.high, data.low
    gaps: list[FairValueGap] = []

    for i in range(1, len(high) - 1):
        later_low = low[i + 2:]
        later_high = high[i + 2:]

        if high[i - 1] < low[i + 1] and low[i + 1] - high[i - 1] >= min_size:
            gap_low, gap_high = high[i - 1], low[i + 1]
            gaps.append(FairValueGap(
                "bullish", i, data.time[i], float(gap_low), float(gap_high),
                filled=bool((later_low <= gap_low).any()),
            ))
        elif low[i - 1] > high[i + 1] and low[i - 1] - high[i + 1] >= min_size:
            gap_low, gap_high = high[i + 1], low[i - 1]
            gaps.append(FairValueGap(
                "bearish", i, data.time[i], float(gap_low), float(gap_high),
                filled=bool((later_high >= gap_high).any()),
            ))
    return gaps
