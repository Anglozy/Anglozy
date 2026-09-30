"""Turn detected ICT concepts into trade setups, reports and orders.

Two layers:

* :func:`find_setup` is pure. Given HTF/LTF bars and the current bid/ask it
  returns a fully specified :class:`TradeSignal` (entry, stop, targets that all
  meet ``min_rr``) or the reason there is none.
* :class:`SignalPipeline` is the orchestration layer. It fetches bars through
  ``MT5Connector``, sizes the trade with ``MT5Executor.calculate_lot_size``,
  renders an Aurum report with that exact lot size, and places the limit order.

Setup model (buy side; sells mirror it):
    1. HTF bias is bullish (last HTF structure break was bullish).
    2. On the LTF, a completed Asia/London session low is swept (wick below,
       close back inside).
    3. Within ``max_bars_sweep_to_break`` bars, price closes above a swing high
       (BOS/CHoCH).
    4. The displacement leg leaves a bullish FVG. Entry is a buy limit at the
       FVG midpoint (or top), stop below the sweep wick / order block, targets at
       untaken buy-side liquidity at least ``min_rr`` R away.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pandas as pd

from aurum.capturer import ChartCapturer
from aurum.generator import (
    AurumReportGenerator,
    HTFAnalysis,
    LTFAnalysis,
    PriceZone,
    RiskProfile,
    TakeProfit,
    TradeReport,
    TradeSetup,
)
from config.strategy import StrategyParameters
from mt5.connector import AccountSnapshot, MT5Connector, SymbolSpec, Tick
from mt5.executor import MT5Executor, OrderResult, OrderValidationError, RiskTooSmallError

from .bars import Direction, ohlc
from .fvg import FairValueGap, detect_fvgs
from .liquidity import LiquiditySweep, detect_session_sweeps, session_ranges
from .order_block import OrderBlock, detect_order_blocks
from .structure import Bias, StructureBreak, detect_structure_breaks, unbroken_swings

logger = logging.getLogger(__name__)

Side = Literal["BUY", "SELL"]


# --------------------------------------------------------------------------- #
# Signal data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Target:
    label: str
    price: float
    rr: float


@dataclass(frozen=True)
class TradeSignal:
    direction: Side
    entry: float
    stop_loss: float
    targets: tuple[Target, ...]
    htf_bias: Bias
    htf_timeframe: str
    ltf_timeframe: str
    sweep: LiquiditySweep
    structure_break: StructureBreak
    fvg: FairValueGap
    order_block: OrderBlock | None = None
    htf_break: StructureBreak | None = None
    htf_order_block: OrderBlock | None = None

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def primary_rr(self) -> float:
        return self.targets[0].rr


@dataclass(frozen=True)
class SetupResult:
    signal: TradeSignal | None
    reason: str


# --------------------------------------------------------------------------- #
# Pure setup builder
# --------------------------------------------------------------------------- #
def find_setup(
    htf_bars: pd.DataFrame,
    ltf_bars: pd.DataFrame,
    bid: float,
    ask: float,
    params: StrategyParameters | None = None,
    utc_offset_hours: int = 0,
    tick_size: float | None = None,
) -> SetupResult:
    """Return the most recent valid setup, or ``SetupResult(None, reason)``.

    ``tick_size`` rounds levels to valid prices, always in the direction that
    keeps R:R at or above ``min_rr``.
    """
    params = params or StrategyParameters()

    htf_breaks = detect_structure_breaks(htf_bars, params.htf_swing_lookback)
    if not htf_breaks:
        return SetupResult(None, f"{params.htf_timeframe} bias is neutral (no structure break)")
    htf_break = htf_breaks[-1]
    want: Direction = htf_break.direction
    bias: Bias = "Bullish" if want == "bullish" else "Bearish"
    htf_obs = [ob for ob in detect_order_blocks(htf_bars, params.htf_swing_lookback, htf_breaks)
               if ob.direction == want]
    htf_ob = next((ob for ob in reversed(htf_obs) if not ob.mitigated), htf_obs[-1] if htf_obs else None)

    last = len(ltf_bars) - 1
    sweeps = [
        s for s in detect_session_sweeps(ltf_bars, params.sessions, utc_offset_hours, params.sweep_window_bars)
        if s.direction == want and last - s.index <= params.max_bars_since_sweep
    ]
    if not sweeps:
        side = "low" if want == "bullish" else "high"
        return SetupResult(None, f"{bias} bias but no recent session {side} sweep on {params.ltf_timeframe}")

    ltf_breaks = detect_structure_breaks(ltf_bars, params.ltf_swing_lookback)
    breaks = [b for b in ltf_breaks if b.direction == want]
    blocks = {ob.break_index: ob for ob in detect_order_blocks(ltf_bars, params.ltf_swing_lookback, breaks)}
    fvgs = [g for g in detect_fvgs(ltf_bars, params.min_fvg_size) if g.direction == want]

    reason = "no setup"
    for sweep in reversed(sweeps):
        brk = next(
            (b for b in breaks if sweep.index < b.index <= sweep.index + params.max_bars_sweep_to_break),
            None,
        )
        if brk is None:
            reason = f"{sweep.session} {sweep.side} swept but no {want} structure break followed"
            continue
        leg = [g for g in fvgs if sweep.index < g.index <= brk.index + 1]
        if not leg:
            reason = f"{want.capitalize()} {brk.kind} after sweep left no FVG >= {params.min_fvg_size}"
            continue
        return _build_signal(
            htf_bars, ltf_bars, bid, ask, params, utc_offset_hours, tick_size,
            bias, htf_break, htf_ob, sweep, brk, leg[-1], blocks.get(brk.index),
        )
    return SetupResult(None, reason)


def _build_signal(
    htf_bars: pd.DataFrame,
    ltf_bars: pd.DataFrame,
    bid: float,
    ask: float,
    params: StrategyParameters,
    utc_offset_hours: int,
    tick_size: float | None,
    bias: Bias,
    htf_break: StructureBreak,
    htf_ob: OrderBlock | None,
    sweep: LiquiditySweep,
    brk: StructureBreak,
    fvg: FairValueGap,
    ob: OrderBlock | None,
) -> SetupResult:
    is_buy = bias == "Bullish"
    toward_sl = "down" if is_buy else "up"
    away_from_sl = "up" if is_buy else "down"

    # Entry
    if params.entry_mode == "fvg_mid":
        raw_entry = fvg.midpoint
    else:
        raw_entry = fvg.high if is_buy else fvg.low
    entry = _to_tick(raw_entry, tick_size, toward_sl)

    # Stop beyond the swept wick (and the order block, if it extends further)
    if is_buy:
        protective = min(sweep.extreme, ob.low) if ob else sweep.extreme
        stop = _to_tick(protective - params.sl_buffer, tick_size, "down")
    else:
        protective = max(sweep.extreme, ob.high) if ob else sweep.extreme
        stop = _to_tick(protective + params.sl_buffer, tick_size, "up")
    risk = abs(entry - stop)
    if risk <= 0:
        return SetupResult(None, "Stop loss is not beyond entry")
    if risk > params.max_sl_distance:
        return SetupResult(None, f"Stop distance {risk:.2f} exceeds max {params.max_sl_distance}")

    # The setup must still be untouched
    data = ohlc(ltf_bars)
    after = slice(fvg.index + 2, None)
    if is_buy and (data.low[after] <= entry).any():
        return SetupResult(None, f"Price already traded back to entry {entry} after the FVG formed")
    if not is_buy and (data.high[after] >= entry).any():
        return SetupResult(None, f"Price already traded back to entry {entry} after the FVG formed")
    if is_buy and ask <= entry:
        return SetupResult(None, f"Ask {ask} is at or below entry {entry}; buy limit not possible")
    if not is_buy and bid >= entry:
        return SetupResult(None, f"Bid {bid} is at or above entry {entry}; sell limit not possible")

    targets = _liquidity_targets(
        htf_bars, ltf_bars, params, utc_offset_hours, tick_size, is_buy, entry, risk, fvg.index,
    )
    if not targets:
        price = _to_tick(entry + (1 if is_buy else -1) * params.min_rr * risk, tick_size, away_from_sl)
        targets = [Target(f"Fixed {params.min_rr:g}R", price, abs(price - entry) / risk)]

    extreme_since = data.high[after].max() if is_buy else data.low[after].min()
    if len(data.high[after]) and (
        (is_buy and extreme_since >= targets[0].price) or (not is_buy and extreme_since <= targets[0].price)
    ):
        return SetupResult(None, f"First target {targets[0].price} already reached")

    signal = TradeSignal(
        direction="BUY" if is_buy else "SELL",
        entry=entry,
        stop_loss=stop,
        targets=tuple(targets),
        htf_bias=bias,
        htf_timeframe=params.htf_timeframe,
        ltf_timeframe=params.ltf_timeframe,
        sweep=sweep,
        structure_break=brk,
        fvg=fvg,
        order_block=ob,
        htf_break=htf_break,
        htf_order_block=htf_ob,
    )
    return SetupResult(signal, f"{signal.direction} setup at {entry} (1:{signal.primary_rr:.2f})")


def _liquidity_targets(
    htf_bars: pd.DataFrame,
    ltf_bars: pd.DataFrame,
    params: StrategyParameters,
    utc_offset_hours: int,
    tick_size: float | None,
    is_buy: bool,
    entry: float,
    risk: float,
    fvg_index: int,
) -> list[Target]:
    """Untaken liquidity beyond entry that is at least ``min_rr`` away."""
    kind = "high" if is_buy else "low"
    data = ohlc(ltf_bars)
    candidates: list[tuple[str, float]] = []

    for swing in unbroken_swings(ltf_bars, params.ltf_swing_lookback, kind):
        candidates.append((f"{params.ltf_timeframe} swing {kind}", swing.price))
    for swing in unbroken_swings(htf_bars, params.htf_swing_lookback, kind):
        candidates.append((f"{params.htf_timeframe} swing {kind}", swing.price))
    for rng in session_ranges(ltf_bars, params.sessions, utc_offset_hours):
        later = slice(rng.end_index + 1, None)
        level = rng.high if is_buy else rng.low
        taken = (data.high[later] > level).any() if is_buy else (data.low[later] < level).any()
        if not taken:
            candidates.append((f"{rng.name} {kind}", level))

    targets: list[Target] = []
    direction = 1 if is_buy else -1
    for label, level in sorted(candidates, key=lambda c: c[1] * direction):
        price = _to_tick(level, tick_size, "up" if is_buy else "down")
        rr = (price - entry) * direction / risk
        if rr < params.min_rr - 1e-9:
            continue
        if targets and abs(price - targets[-1].price) < 0.25 * risk:
            continue  # too close to the previous target to be worth a separate TP
        targets.append(Target(label, price, rr))
        if len(targets) == params.max_targets:
            break
    return targets


def _to_tick(price: float, tick_size: float | None, how: Literal["up", "down", "nearest"]) -> float:
    if not tick_size:
        return price
    steps = price / tick_size
    if how == "up":
        k = math.ceil(steps - 1e-9)
    elif how == "down":
        k = math.floor(steps + 1e-9)
    else:
        k = round(steps)
    return round(k * tick_size, 10)


# --------------------------------------------------------------------------- #
# Report mapping
# --------------------------------------------------------------------------- #
def build_trade_report(
    signal: TradeSignal,
    symbol: str,
    lot_size: float,
    account: AccountSnapshot,
    spec: SymbolSpec,
    risk_percent: float,
    htf_chart: str | Path | None = None,
    ltf_chart: str | Path | None = None,
) -> TradeReport:
    """Map a signal plus the MT5-calculated lot size (and optional chart
    screenshots) onto an Aurum report."""
    d = spec.digits
    fmt = lambda x: f"{x:.{d}f}"  # noqa: E731
    is_buy = signal.direction == "BUY"
    word = "above" if is_buy else "below"

    order_blocks = []
    if signal.order_block:
        ob = signal.order_block
        order_blocks.append(PriceZone(f"{signal.ltf_timeframe} {ob.direction.capitalize()} Order Block", ob.low, ob.high))
    if signal.htf_order_block:
        ob = signal.htf_order_block
        order_blocks.append(PriceZone(f"{signal.htf_timeframe} {ob.direction.capitalize()} Order Block", ob.low, ob.high))

    htf_break = signal.htf_break
    htf_ob = signal.htf_order_block
    sweep, brk, fvg = signal.sweep, signal.structure_break, signal.fvg

    return TradeReport(
        setup=TradeSetup(
            pair=symbol,
            direction=signal.direction,
            order_type="Buy limit" if is_buy else "Sell limit",
            entry=signal.entry,
            stop_loss=signal.stop_loss,
            fvg_low=fvg.low,
            fvg_high=fvg.high,
            order_blocks=order_blocks,
            take_profits=[
                TakeProfit(f"TP{i} · {t.label}", t.price) for i, t in enumerate(signal.targets, start=1)
            ],
            invalidation=f"Price trades {'below' if is_buy else 'above'} {fmt(signal.stop_loss)} "
                         f"(beyond the swept {sweep.session} {sweep.side}).",
        ),
        htf=HTFAnalysis(
            timeframe=signal.htf_timeframe,
            bias=signal.htf_bias,
            structure=f"{htf_break.kind} {word} {fmt(htf_break.level)}" if htf_break else "—",
            key_level=f"{signal.htf_timeframe} OB {fmt(htf_ob.low)} – {fmt(htf_ob.high)}" if htf_ob else "—",
            draw_on_liquidity=f"{signal.targets[-1].label} at {fmt(signal.targets[-1].price)}",
            notes=f"Only {signal.direction} setups are taken while the {signal.htf_timeframe} bias is {signal.htf_bias.lower()}.",
        ),
        ltf=LTFAnalysis(
            timeframe=signal.ltf_timeframe,
            bias=signal.htf_bias,
            trigger=f"Retrace into {signal.ltf_timeframe} FVG {fmt(fvg.low)} – {fmt(fvg.high)}",
            confirmation=f"{brk.kind} {word} {fmt(brk.level)}",
            liquidity_sweep=f"{sweep.session} {sweep.side} {fmt(sweep.level)} swept to {fmt(sweep.extreme)}",
            notes=f"{signal.direction.capitalize()} limit inside the FVG; stop beyond the "
                  f"{sweep.session} {sweep.side} sweep wick.",
        ),
        risk=RiskProfile(
            account_balance=account.balance,
            risk_percent=risk_percent,
            lot_size=lot_size,
            contract_size=spec.tick_value / spec.tick_size,  # money per 1.0 price move per lot
            lot_step=spec.volume_step,
            min_lot=spec.volume_min,
            max_lot=spec.volume_max,
            currency=account.currency,
        ),
        htf_chart=htf_chart,
        ltf_chart=ltf_chart,
        strategy_name="ICT · Sweep + FVG + OB",
        digits=d,
    )


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
PipelineStatus = Literal["no_setup", "skipped", "reported", "executed", "order_failed"]


@dataclass
class PipelineResult:
    status: PipelineStatus
    message: str
    signal: TradeSignal | None = None
    lot_size: float | None = None
    report_path: Path | None = None
    order: OrderResult | None = None
    chart_paths: tuple[str | None, str | None] = (None, None)  # HTF, LTF screenshots


class SignalPipeline:
    """Fetch bars -> find setup -> size with MT5 -> chart screenshots -> Aurum report -> place order."""

    def __init__(
        self,
        connector: MT5Connector,
        executor: MT5Executor,
        report_generator: AurumReportGenerator,
        params: StrategyParameters | None = None,
        execute: bool = True,
        chart_capturer: ChartCapturer | None = None,
    ) -> None:
        self.connector = connector
        self.executor = executor
        self.report_generator = report_generator
        self.params = params or StrategyParameters()
        self.execute = execute
        self.chart_capturer = chart_capturer
        self._last_signal_key: tuple | None = None

    def run(self, symbol: str | None = None) -> PipelineResult:
        p = self.params
        symbol = symbol or self.executor.settings.symbol

        htf = self.connector.get_bars(symbol, p.htf_timeframe, p.htf_bars)
        ltf = self.connector.get_bars(symbol, p.ltf_timeframe, p.ltf_bars)
        tick = self.connector.get_tick(symbol)
        spec = self.connector.symbol_spec(symbol)
        offset = self._utc_offset(tick)

        found = find_setup(htf, ltf, tick.bid, tick.ask, p, offset, spec.tick_size)
        if found.signal is None:
            logger.info("%s: no setup: %s", symbol, found.reason)
            return PipelineResult("no_setup", found.reason)
        signal = found.signal
        logger.info(
            "%s: %s entry=%s sl=%s tps=%s",
            symbol, found.reason, signal.entry, signal.stop_loss, [t.price for t in signal.targets],
        )

        key = (symbol, signal.direction, str(signal.fvg.time), signal.entry)
        if key == self._last_signal_key:
            return PipelineResult("skipped", "Setup already processed", signal)
        if self.executor.open_positions(symbol) or self.executor.pending_orders(symbol):
            msg = f"Existing {symbol} position or pending order with magic {self.executor.settings.magic_number}"
            logger.info("%s; not stacking another trade", msg)
            return PipelineResult("skipped", msg, signal)

        try:
            lot = self.executor.calculate_lot_size(signal.entry, signal.stop_loss, symbol)
        except RiskTooSmallError as exc:
            logger.warning("%s: %s", symbol, exc)
            return PipelineResult("skipped", str(exc), signal)

        charts = self._capture_charts(symbol)
        report_path = self._write_report(signal, symbol, lot, spec, charts)

        if not self.execute:
            self._last_signal_key = key
            return PipelineResult("reported", "Dry run: report generated, no order sent",
                                  signal, lot, report_path, chart_paths=charts)

        expiration = None
        if p.order_expiry_minutes:
            # MT5 compares expiration with server time, which is what tick.time holds.
            expiration = tick.time + timedelta(minutes=p.order_expiry_minutes)
        try:
            order = self.executor.place_limit_order(
                signal.direction,
                price=signal.entry,
                stop_loss=signal.stop_loss,
                take_profit=signal.targets[0].price,
                volume=lot,
                symbol=symbol,
                expiration=expiration,
                comment=p.order_comment,
            )
        except OrderValidationError as exc:
            logger.error("%s order not sent: %s", symbol, exc)
            return PipelineResult("order_failed", str(exc), signal, lot, report_path, chart_paths=charts)

        if order.success:
            self._last_signal_key = key
            return PipelineResult("executed", f"Order {order.order} placed ({order.retcode_name})",
                                  signal, lot, report_path, order, charts)
        return PipelineResult("order_failed", order.message, signal, lot, report_path, order, charts)

    def _capture_charts(self, symbol: str) -> tuple[str | None, str | None]:
        """Screenshot the HTF and LTF charts. Failures are logged and yield None:
        a missing screenshot must never block a valid trade."""
        if self.chart_capturer is None:
            return None, None
        try:
            return asyncio.run(self._capture_both(symbol))
        except Exception:
            logger.exception("Chart capture failed; the report will show placeholders")
            return None, None

    async def _capture_both(self, symbol: str) -> tuple[str | None, str | None]:
        p = self.params
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        timeframes = (p.htf_timeframe, p.ltf_timeframe)
        async with self.chart_capturer as capturer:
            results = await asyncio.gather(
                *(capturer.capture_chart(symbol, tf, f"{symbol}_{tf}_{stamp}.png".lower()) for tf in timeframes),
                return_exceptions=True,
            )
        paths: list[str | None] = []
        for tf, result in zip(timeframes, results):
            if isinstance(result, BaseException):
                logger.warning("%s %s chart not captured: %s", symbol, tf, result)
                paths.append(None)
            else:
                paths.append(result)
        return paths[0], paths[1]

    def _write_report(
        self,
        signal: TradeSignal,
        symbol: str,
        lot: float,
        spec: SymbolSpec,
        charts: tuple[str | None, str | None] = (None, None),
    ) -> Path | None:
        try:
            report = build_trade_report(
                signal, symbol, lot, self.connector.account_info(), spec, self.executor.settings.risk_percent,
                htf_chart=charts[0], ltf_chart=charts[1],
            )
            path = self.report_generator.generate(report)
            logger.info("Aurum report: %s", path)
            return path
        except Exception:  # a report failure must not block a valid trade
            logger.exception("Could not generate Aurum report")
            return None

    def _utc_offset(self, tick: Tick) -> int:
        if self.params.server_utc_offset_hours is not None:
            return self.params.server_utc_offset_hours
        hours = (tick.time - datetime.now(timezone.utc)).total_seconds() / 3600
        offset = round(hours)
        if -12 <= offset <= 14:
            logger.debug("Estimated broker server offset UTC%+d", offset)
            return offset
        logger.warning(
            "Latest tick is stale (%.1fh off); assuming server time is UTC. "
            "Set server_utc_offset_hours to silence this.", hours,
        )
        return 0
