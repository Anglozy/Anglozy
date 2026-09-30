"""Strategy parameters for the ICT FVG / Order Block / liquidity sweep model.

All prices and distances are in the symbol's price units (for XAUUSD, 1.0 = $1
move per ounce). Session hours are UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EntryMode = Literal["fvg_mid", "fvg_edge"]

# Session windows in UTC hours: (start inclusive, end exclusive).
DEFAULT_SESSIONS: dict[str, tuple[int, int]] = {
    "Asia": (0, 6),
    "London": (7, 12),
}


@dataclass(frozen=True)
class StrategyParameters:
    # Timeframes and history
    htf_timeframe: str = "H4"
    ltf_timeframe: str = "M15"
    htf_bars: int = 300
    ltf_bars: int = 500

    # Market structure: a swing needs this many lower highs / higher lows on each side
    htf_swing_lookback: int = 3
    ltf_swing_lookback: int = 2

    # Fair Value Gaps
    min_fvg_size: float = 0.50
    # "fvg_mid": enter at the gap's midpoint (consequent encroachment);
    # "fvg_edge": enter at the first touch of the gap (top for buys, bottom for sells).
    entry_mode: EntryMode = "fvg_mid"

    # Liquidity sweeps
    sessions: dict[str, tuple[int, int]] = field(default_factory=lambda: dict(DEFAULT_SESSIONS))
    sweep_window_bars: int = 48  # how long after a session closes its levels can be swept
    max_bars_since_sweep: int = 48  # ignore sweeps older than this many LTF bars
    max_bars_sweep_to_break: int = 16  # the structure break must follow the sweep quickly

    # Risk rules
    min_rr: float = 2.0  # every take profit must be at least this many R
    max_targets: int = 3
    sl_buffer: float = 0.50  # added beyond the sweep wick / order block
    max_sl_distance: float = 25.0  # reject setups with wider stops

    # Broker server time = UTC + offset. MT5 bar times are server time; many
    # brokers run UTC+2/UTC+3. None = estimate from the latest tick.
    server_utc_offset_hours: int | None = None

    # Execution
    order_expiry_minutes: int | None = None  # None = good till cancelled
    order_comment: str = "aurum ict"

    def __post_init__(self) -> None:
        if self.min_rr < 1:
            raise ValueError("min_rr must be >= 1")
        if self.max_targets < 1:
            raise ValueError("max_targets must be >= 1")
        if self.entry_mode not in ("fvg_mid", "fvg_edge"):
            raise ValueError(f"unknown entry_mode {self.entry_mode!r}")
        for name, (start, end) in self.sessions.items():
            if not 0 <= start < end <= 24:
                raise ValueError(f"session {name!r} must satisfy 0 <= start < end <= 24 (UTC hours)")
