"""Swing points, market structure breaks (BOS / CHoCH) and directional bias."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .bars import Direction, ohlc

Bias = Literal["Bullish", "Bearish", "Neutral"]


@dataclass(frozen=True)
class SwingPoint:
    kind: Literal["high", "low"]
    index: int
    price: float


@dataclass(frozen=True)
class StructureBreak:
    direction: Direction
    kind: Literal["BOS", "CHoCH"]  # BOS continues the trend, CHoCH reverses it
    index: int  # candle whose close broke the swing
    time: pd.Timestamp
    level: float  # the swing price that was broken
    swing_index: int


def find_swings(bars: pd.DataFrame, lookback: int = 2) -> list[SwingPoint]:
    """Fractal swing highs/lows: the extreme of ``lookback`` candles on each side.

    A swing is only known ``lookback`` candles after it forms, so the last
    ``lookback`` candles can never be swings.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    data = ohlc(bars)
    high, low = data.high, data.low
    swings: list[SwingPoint] = []
    for i in range(lookback, len(high) - lookback):
        left_h, right_h = high[i - lookback:i], high[i + 1:i + lookback + 1]
        if high[i] > left_h.max() and high[i] >= right_h.max():
            swings.append(SwingPoint("high", i, float(high[i])))
        left_l, right_l = low[i - lookback:i], low[i + 1:i + lookback + 1]
        if low[i] < left_l.min() and low[i] <= right_l.min():
            swings.append(SwingPoint("low", i, float(low[i])))
    return swings


def detect_structure_breaks(bars: pd.DataFrame, lookback: int = 2) -> list[StructureBreak]:
    """Candle closes beyond the most recent confirmed swing, oldest first.

    A bullish break is a close above the latest swing high; bearish, a close
    below the latest swing low. Each swing can be broken once. The first break
    against the prevailing direction is a CHoCH; breaks with it are BOS.
    """
    data = ohlc(bars)
    close = data.close
    confirmed_at: dict[int, list[SwingPoint]] = {}
    for swing in find_swings(bars, lookback):
        confirmed_at.setdefault(swing.index + lookback, []).append(swing)

    breaks: list[StructureBreak] = []
    last_high: SwingPoint | None = None
    last_low: SwingPoint | None = None
    trend: Direction | None = None

    for j in range(len(close)):
        for swing in confirmed_at.get(j, ()):
            if swing.kind == "high":
                last_high = swing
            else:
                last_low = swing

        if last_high is not None and close[j] > last_high.price:
            kind = "BOS" if trend == "bullish" else "CHoCH"
            breaks.append(StructureBreak("bullish", kind, j, data.time[j], last_high.price, last_high.index))
            trend, last_high = "bullish", None
        elif last_low is not None and close[j] < last_low.price:
            kind = "BOS" if trend == "bearish" else "CHoCH"
            breaks.append(StructureBreak("bearish", kind, j, data.time[j], last_low.price, last_low.index))
            trend, last_low = "bearish", None
    return breaks


def market_bias(bars: pd.DataFrame, lookback: int = 3) -> Bias:
    """Direction of the most recent structure break (Neutral if none)."""
    breaks = detect_structure_breaks(bars, lookback)
    if not breaks:
        return "Neutral"
    return "Bullish" if breaks[-1].direction == "bullish" else "Bearish"


def unbroken_swings(bars: pd.DataFrame, lookback: int, kind: Literal["high", "low"]) -> list[SwingPoint]:
    """Swing highs never traded above since (buy-side liquidity), or swing lows
    never traded below (sell-side liquidity)."""
    data = ohlc(bars)
    result = []
    for swing in find_swings(bars, lookback):
        if swing.kind != kind:
            continue
        later = data.high[swing.index + 1:] if kind == "high" else data.low[swing.index + 1:]
        taken = (later > swing.price).any() if kind == "high" else (later < swing.price).any()
        if not taken:
            result.append(swing)
    return result
