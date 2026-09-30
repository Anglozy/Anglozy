"""Shared helpers for reading OHLC bar DataFrames (as returned by MT5Connector.get_bars)."""

from __future__ import annotations

from typing import Literal, NamedTuple

import numpy as np
import pandas as pd

Direction = Literal["bullish", "bearish"]

REQUIRED_COLUMNS = ("time", "open", "high", "low", "close")


class OHLC(NamedTuple):
    time: pd.Series
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray


def ohlc(bars: pd.DataFrame) -> OHLC:
    """Validate ``bars`` and return its columns as arrays (oldest bar first)."""
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars is missing columns: {', '.join(missing)}")
    bars = bars.reset_index(drop=True)
    return OHLC(
        time=bars["time"],
        open=bars["open"].to_numpy(dtype=float),
        high=bars["high"].to_numpy(dtype=float),
        low=bars["low"].to_numpy(dtype=float),
        close=bars["close"].to_numpy(dtype=float),
    )
