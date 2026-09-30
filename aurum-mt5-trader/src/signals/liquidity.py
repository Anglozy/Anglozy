"""Session ranges (Asia, London) and sweeps of their highs/lows."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from config.strategy import DEFAULT_SESSIONS

from .bars import Direction, ohlc


@dataclass(frozen=True)
class SessionRange:
    name: str
    date: dt.date  # UTC date
    start_index: int
    end_index: int  # last bar inside the session
    high: float
    low: float


@dataclass(frozen=True)
class LiquiditySweep:
    session: str
    date: dt.date
    side: Literal["high", "low"]  # which session extreme was swept
    direction: Direction  # expected reversal: high swept -> bearish, low swept -> bullish
    index: int  # sweeping candle
    time: pd.Timestamp
    level: float  # session high/low that was taken
    extreme: float  # wick beyond the level


def session_ranges(
    bars: pd.DataFrame,
    sessions: dict[str, tuple[int, int]] | None = None,
    utc_offset_hours: int = 0,
) -> list[SessionRange]:
    """High/low of each *completed* session in ``bars``, oldest first.

    ``utc_offset_hours`` is the broker server offset: MT5 bar times are server
    time, so ``bar time - offset`` is UTC. Session hours are UTC.
    """
    sessions = DEFAULT_SESSIONS if sessions is None else sessions
    data = ohlc(bars)
    utc = pd.to_datetime(data.time) - pd.Timedelta(hours=utc_offset_hours)
    hours = utc.dt.hour.to_numpy()
    dates = utc.dt.date.to_numpy()

    def session_of(i: int) -> str | None:
        for name, (start, end) in sessions.items():
            if start <= hours[i] < end:
                return name
        return None

    ranges: list[SessionRange] = []
    n = len(hours)
    i = 0
    while i < n:
        name = session_of(i)
        if name is None:
            i += 1
            continue
        j = i
        while j + 1 < n and session_of(j + 1) == name and dates[j + 1] == dates[i]:
            j += 1
        if j + 1 < n:  # completed: a later bar exists outside this session instance
            ranges.append(SessionRange(
                name, dates[i], i, j,
                float(data.high[i:j + 1].max()), float(data.low[i:j + 1].min()),
            ))
        i = j + 1
    return ranges


def detect_session_sweeps(
    bars: pd.DataFrame,
    sessions: dict[str, tuple[int, int]] | None = None,
    utc_offset_hours: int = 0,
    max_bars_after: int = 48,
) -> list[LiquiditySweep]:
    """Sweeps of completed session highs/lows, ordered by sweeping candle.

    After a session ends, the first candle to trade beyond its high (or low) is
    checked: if it closes back inside the range, the level was swept (a stop
    run). If it closes beyond, it was a breakout and that side is no longer
    watched. Only the ``max_bars_after`` candles following the session count.
    """
    data = ohlc(bars)
    h, l, c = data.high, data.low, data.close
    sweeps: list[LiquiditySweep] = []

    for rng in session_ranges(bars, sessions, utc_offset_hours):
        high_done = low_done = False
        stop = min(len(h), rng.end_index + 1 + max_bars_after)
        for k in range(rng.end_index + 1, stop):
            if not high_done and h[k] > rng.high:
                high_done = True
                if c[k] < rng.high:
                    sweeps.append(LiquiditySweep(
                        rng.name, rng.date, "high", "bearish", k, data.time[k], rng.high, float(h[k]),
                    ))
            if not low_done and l[k] < rng.low:
                low_done = True
                if c[k] > rng.low:
                    sweeps.append(LiquiditySweep(
                        rng.name, rng.date, "low", "bullish", k, data.time[k], rng.low, float(l[k]),
                    ))
            if high_done and low_done:
                break
    return sorted(sweeps, key=lambda s: s.index)
