"""Hand-built OHLC data with known answers for signal and pipeline tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

RATE_DTYPE = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
              ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]


def make_bars(rows, start="2026-09-29 00:00", freq="15min") -> pd.DataFrame:
    """rows: iterable of (open, high, low, close)."""
    rows = list(rows)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], dtype=float)
    df.insert(0, "time", pd.date_range(start, periods=len(rows), freq=freq, tz="UTC"))
    return df


def bars_from_closes(closes, start="2026-09-29 00:00", freq="15min", wick=0.5) -> pd.DataFrame:
    """Each bar opens at the previous close; wicks extend ``wick`` beyond the body."""
    rows, prev = [], closes[0]
    for c in closes:
        o = prev
        rows.append((o, max(o, c) + wick, min(o, c) - wick, c))
        prev = c
    return make_bars(rows, start, freq)


def mirror(bars: pd.DataFrame, pivot: float = 2650.0) -> pd.DataFrame:
    """Reflect prices around ``pivot`` so a bullish scenario becomes bearish."""
    out = bars.copy()
    out["open"] = 2 * pivot - bars["open"]
    out["close"] = 2 * pivot - bars["close"]
    out["high"] = 2 * pivot - bars["low"]
    out["low"] = 2 * pivot - bars["high"]
    return out


def to_rates(bars: pd.DataFrame, add_forming_bar: bool = True) -> np.ndarray:
    """Convert to the structured array ``copy_rates_from_pos`` returns.

    MT5Connector drops the still-forming bar, so by default a copy of the last
    bar is appended to play that role.
    """
    df = bars.copy()
    if add_forming_bar:
        extra = df.iloc[[-1]].copy()
        extra["time"] = extra["time"] + (df["time"].iloc[-1] - df["time"].iloc[-2])
        df = pd.concat([df, extra], ignore_index=True)
    seconds = (df["time"] - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)
    rows = [(int(t), o, h, l, c, 100, 30, 0)
            for t, o, h, l, c in zip(seconds, df["open"], df["high"], df["low"], df["close"])]
    return np.array(rows, dtype=RATE_DTYPE)


# --------------------------------------------------------------------------- #
# Bullish XAUUSD scenario
# --------------------------------------------------------------------------- #
# H4: rally with a bullish CHoCH at bar 9 (close 2628 > swing high 2621 at bar 4),
# peak 2691 at bar 16, lower high 2672 at bar 23, holds above swing low 2651.
# Untaken buy-side liquidity: 2672 and 2691. Bias: Bullish.
HTF_CLOSES = [
    2600, 2605, 2610, 2615, 2620, 2612, 2606, 2612, 2620, 2628,
    2636, 2645, 2655, 2665, 2675, 2685, 2690, 2680, 2668, 2658,
    2652, 2660, 2668, 2671, 2662, 2658, 2656, 2655, 2657,
]

EXPECTED_BUY = {
    "entry": 2646.50,       # midpoint of FVG 2645-2648
    "stop_loss": 2636.50,   # sweep wick 2637 - 0.50 buffer
    "targets": [2672.0, 2691.0],  # H4 swing highs; 2.55R and 4.45R
}


def htf_bullish() -> pd.DataFrame:
    return bars_from_closes(HTF_CLOSES, start="2026-09-24 00:00", freq="4h", wick=1.0)


def ltf_bullish() -> pd.DataFrame:
    """M15 bars for 2026-09-29 (UTC).

    * 00:00-05:45 Asia: range 2640-2650 (high at bar 6, low at bar 18)
    * 07:15 London (bar 29): wick to 2637 sweeps the Asia low, closes 2641
    * bar 31 displacement closes 2650.8 above the 2650 swing high -> bullish CHoCH
    * bars 30/32 leave a bullish FVG 2645-2648; price holds above it afterwards
    """
    flat = (2645.0, 2646.0, 2644.0, 2645.0)
    rows = [flat] * 28
    rows[6] = (2645.0, 2650.0, 2644.0, 2646.0)   # Asia high
    rows[18] = (2645.0, 2646.0, 2640.0, 2644.5)  # Asia low
    rows += [
        (2645.0, 2645.5, 2642.0, 2642.5),   # 28 07:00
        (2642.5, 2643.0, 2637.0, 2641.0),   # 29 sweep of Asia low, bearish OB candle
        (2641.0, 2645.0, 2640.5, 2644.5),   # 30 FVG candle 1 (high 2645)
        (2644.5, 2651.0, 2644.0, 2650.8),   # 31 displacement, CHoCH above 2650
        (2650.8, 2654.0, 2648.0, 2653.5),   # 32 FVG candle 3 (low 2648)
        (2653.5, 2655.0, 2651.0, 2654.0),
        (2654.0, 2656.0, 2652.0, 2655.0),
        (2655.0, 2655.5, 2652.5, 2653.0),
        (2653.0, 2654.0, 2651.5, 2652.0),
        (2652.0, 2654.5, 2651.0, 2654.0),
        (2654.0, 2655.0, 2652.5, 2654.5),
        (2654.5, 2655.0, 2653.0, 2654.0),   # 39 last closed bar 09:45
    ]
    return make_bars(rows)


BULLISH_BID, BULLISH_ASK = 2654.00, 2654.30
