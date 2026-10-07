"""Thread-safe bot state for the Aurum dashboard API.

Three pieces:

* :class:`PipelineRunner` owns one MT5 session and the same pipeline ``main.py``
  runs (connector -> executor -> chart capturer -> report generator). All MT5
  calls go through one lock, because the MetaTrader5 module is a process-wide
  singleton and the API threads and the scan thread share it.
* :class:`BotStateManager` runs the runner on a background thread, with
  start / pause / resume / stop, and caches the latest account metrics, setup
  result and detector stats so GET endpoints never block on MT5.
* :class:`CredentialStore` reads and updates MT5 credentials in ``.env``
  without ever returning the password.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np
import pandas as pd

from aurum.capturer import ChartCapturer
from aurum.generator import AurumReportGenerator
from config.settings import Settings, load_settings
from config.strategy import StrategyParameters
from mt5.connector import MT5Connector
from mt5.executor import MT5Executor, OrderResult
from risk.manager import RiskManager
from signals.fvg import detect_fvgs
from signals.liquidity import detect_session_sweeps
from signals.order_block import detect_order_blocks
from signals.parser import PipelineResult, SignalPipeline
from signals.structure import detect_structure_breaks

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "cache"

PASSWORD_MASK = "********"


# --------------------------------------------------------------------------- #
# Errors and status
# --------------------------------------------------------------------------- #
class StateError(RuntimeError):
    """The requested transition is not allowed from the current state."""


class NotConnectedError(RuntimeError):
    """The bot has no live MT5 session."""


class BotStatus(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    SCANNING = "scanning"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"


@dataclasses.dataclass(frozen=True)
class BotConfig:
    dry_run: bool = True
    interval_seconds: float = 60.0


# --------------------------------------------------------------------------- #
# Runner: one MT5 session + pipeline
# --------------------------------------------------------------------------- #
class Runner(Protocol):
    def start(self) -> dict[str, Any]: ...
    def run_once(self) -> dict[str, Any]: ...
    def account(self) -> dict[str, Any]: ...
    def positions(self) -> dict[str, Any]: ...
    def close_ticket(self, ticket: int) -> dict[str, Any]: ...
    def close(self) -> None: ...


class PipelineRunner:
    """Connects to MT5 and runs one :class:`SignalPipeline` pass per call."""

    def __init__(
        self,
        config: BotConfig,
        settings_loader: Callable[[], Settings] = load_settings,
        params: StrategyParameters | None = None,
        reports_dir: Path = DEFAULT_REPORTS_DIR,
        mt5_module: Any | None = None,
        charts: bool | None = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        risk_factory: Callable[[Settings, MT5Connector], RiskManager | None] | None = None,
    ) -> None:
        self.config = config
        self.settings_loader = settings_loader
        self.params = params or StrategyParameters()
        self.reports_dir = Path(reports_dir)
        self.mt5_module = mt5_module
        self.charts = charts
        self.cache_dir = Path(cache_dir)
        self.risk_factory = risk_factory or (
            lambda settings, connector: RiskManager.from_settings(settings.risk, connector, cache_dir=self.cache_dir))
        self.risk: RiskManager | None = None
        self._lock = threading.RLock()
        self.connector: MT5Connector | None = None
        self.executor: MT5Executor | None = None
        self.pipeline: SignalPipeline | None = None
        self.symbol = ""

    def start(self) -> dict[str, Any]:
        settings = self.settings_loader()
        with self._lock:
            connector = MT5Connector(settings.credentials, settings.trading, mt5_module=self.mt5_module)
            connector.connect()
            capturer = None
            if (settings.charts.enabled if self.charts is None else self.charts):
                capturer = ChartCapturer(
                    images_dir=self.reports_dir / "images",
                    exchange=settings.charts.exchange,
                    executable_path=settings.charts.chromium_path,
                )
            self.connector = connector
            self.executor = MT5Executor(connector)
            self.symbol = settings.trading.symbol
            self.risk = self.risk_factory(settings, connector)
            self.pipeline = SignalPipeline(
                connector,
                self.executor,
                AurumReportGenerator(output_dir=self.reports_dir),
                self.params,
                execute=not self.config.dry_run,
                chart_capturer=capturer,
                risk_manager=self.risk,
            )
            return self._account_locked()

    def run_once(self) -> dict[str, Any]:
        with self._lock:
            pipeline, connector = self._require()
            result = pipeline.run(self.symbol)
            stats = self._detector_stats(connector, pipeline)
            risk = None
            if self.risk is not None:
                risk = self.risk.snapshot(pipeline._utc_offset(connector.get_tick(self.symbol)))
            return {
                "result": summarize_result(result),
                "detectors": stats,
                "account": self._account_locked(),
                "risk": risk,
            }

    def account(self) -> dict[str, Any]:
        with self._lock:
            self._require()
            return self._account_locked()

    def positions(self) -> dict[str, Any]:
        with self._lock:
            self._require()
            positions = self.executor.open_positions(self.symbol)
            orders = self.executor.pending_orders(self.symbol)
            return {
                "symbol": self.symbol,
                "magic": self.executor.settings.magic_number,
                "positions": [_position_dict(p) for p in positions],
                "orders": [_order_dict(o) for o in orders],
            }

    def close_ticket(self, ticket: int) -> dict[str, Any]:
        """Close a bot position, or cancel a bot pending order, by ticket."""
        with self._lock:
            self._require()
            if any(o.ticket == ticket for o in self.executor.pending_orders(self.symbol)):
                return _order_result_dict(self.executor.cancel_order(ticket), "cancel")
            return _order_result_dict(self.executor.close_position(ticket), "close")

    def close(self) -> None:
        with self._lock:
            if self.connector is not None:
                self.connector.disconnect()
            self.connector = self.executor = self.pipeline = self.risk = None

    # ------------------------------------------------------------ helpers
    def _require(self) -> tuple[SignalPipeline, MT5Connector]:
        if self.pipeline is None or self.connector is None:
            raise NotConnectedError("Bot is not connected to MT5")
        return self.pipeline, self.connector

    def _account_locked(self) -> dict[str, Any]:
        a = self.connector.account_info()
        return {
            "login": a.login,
            "server": a.server,
            "name": a.name,
            "currency": a.currency,
            "balance": a.balance,
            "equity": a.equity,
            "floating_pnl": round(a.equity - a.balance, 2),
            "margin_free": a.margin_free,
            "leverage": a.leverage,
            "trade_mode": a.trade_mode,
            "trade_allowed": a.trade_allowed,
        }

    def _detector_stats(self, connector: MT5Connector, pipeline: SignalPipeline) -> dict[str, Any]:
        p = self.params
        htf = connector.get_bars(self.symbol, p.htf_timeframe, p.htf_bars)
        ltf = connector.get_bars(self.symbol, p.ltf_timeframe, p.ltf_bars)
        offset = pipeline._utc_offset(connector.get_tick(self.symbol))
        return detector_stats(htf, ltf, p, offset)


def detector_stats(
    htf: pd.DataFrame, ltf: pd.DataFrame, params: StrategyParameters, utc_offset_hours: int = 0,
) -> dict[str, Any]:
    """Counts and latest instances of each ICT concept on the current bars."""
    htf_breaks = detect_structure_breaks(htf, params.htf_swing_lookback)
    ltf_breaks = detect_structure_breaks(ltf, params.ltf_swing_lookback)
    fvgs = detect_fvgs(ltf, params.min_fvg_size)
    blocks = detect_order_blocks(ltf, params.ltf_swing_lookback, ltf_breaks)
    sweeps = detect_session_sweeps(ltf, params.sessions, utc_offset_hours, params.sweep_window_bars)

    def count(items, **flags):
        return {
            d: sum(1 for x in items if x.direction == d and all(getattr(x, k) == v for k, v in flags.items()))
            for d in ("bullish", "bearish")
        }

    bias = "Neutral"
    if htf_breaks:
        bias = "Bullish" if htf_breaks[-1].direction == "bullish" else "Bearish"
    return to_jsonable({
        "htf_timeframe": params.htf_timeframe,
        "ltf_timeframe": params.ltf_timeframe,
        "htf_bias": bias,
        "htf_last_break": htf_breaks[-1] if htf_breaks else None,
        "ltf_last_break": ltf_breaks[-1] if ltf_breaks else None,
        "bars": {"htf": len(htf), "ltf": len(ltf)},
        "fvgs": {"total": count(fvgs), "unfilled": count(fvgs, filled=False),
                 "latest": [g for g in fvgs if not g.filled][-3:]},
        "order_blocks": {"total": count(blocks), "unmitigated": count(blocks, mitigated=False),
                         "latest": [b for b in blocks if not b.mitigated][-3:]},
        "structure_breaks": {"total": count(ltf_breaks)},
        "sweeps": {"total": count(sweeps), "latest": sweeps[-5:]},
        "utc_offset_hours": utc_offset_hours,
    })


def summarize_result(result: PipelineResult) -> dict[str, Any]:
    """JSON-friendly view of one pipeline pass."""
    out: dict[str, Any] = {
        "status": result.status,
        "message": result.message,
        "lot_size": result.lot_size,
        "report": result.report_path.name if result.report_path else None,
        "risk_guard": result.risk_guard,
        "signal": None,
        "order": None,
    }
    s = result.signal
    if s is not None:
        out["signal"] = to_jsonable({
            "direction": s.direction,
            "entry": s.entry,
            "stop_loss": s.stop_loss,
            "risk": s.risk,
            "targets": [dataclasses.asdict(t) for t in s.targets],
            "htf_bias": s.htf_bias,
            "sweep": s.sweep,
            "structure_break": s.structure_break,
            "fvg": s.fvg,
            "order_block": s.order_block,
        })
    if result.order is not None:
        o = result.order
        out["order"] = {"success": o.success, "retcode": o.retcode, "retcode_name": o.retcode_name,
                        "message": o.message, "ticket": o.order, "volume": o.volume, "price": o.price}
    return out


# --------------------------------------------------------------------------- #
# State manager
# --------------------------------------------------------------------------- #
class BotStateManager:
    """Owns the scan thread and a consistent, lock-protected view of the bot."""

    def __init__(
        self,
        runner_factory: Callable[[BotConfig], Runner] | None = None,
        default_interval: float = 60.0,
    ) -> None:
        self._runner_factory = runner_factory or (lambda cfg: PipelineRunner(cfg))
        self._default_interval = default_interval
        self._lock = threading.RLock()
        self._runs_changed = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._unpaused = threading.Event()
        self._runner: Runner | None = None

        self.status = BotStatus.STOPPED
        self.config = BotConfig(interval_seconds=default_interval)
        self.started_at: dt.datetime | None = None
        self.last_run_at: dt.datetime | None = None
        self.runs = 0
        self.last_error: str | None = None
        self.account: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.detectors: dict[str, Any] | None = None
        self.risk: dict[str, Any] | None = None

    # ------------------------------------------------------------ control
    def start(self, dry_run: bool = True, interval_seconds: float | None = None) -> dict[str, Any]:
        with self._lock:
            if self.status not in (BotStatus.STOPPED, BotStatus.ERROR):
                raise StateError(f"Bot is already {self.status.value}")
            interval = self._default_interval if interval_seconds is None else interval_seconds
            if interval <= 0:
                raise ValueError("interval_seconds must be positive")
            self.config = BotConfig(dry_run=dry_run, interval_seconds=interval)
            self.status = BotStatus.STARTING
            self.started_at = _now()
            self.last_error = None
            self._stop.clear()
            self._unpaused.set()
            self._thread = threading.Thread(target=self._worker, name="aurum-scanner", daemon=True)
            self._thread.start()
            return self.snapshot()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self.status not in (BotStatus.SCANNING, BotStatus.STARTING):
                raise StateError(f"Cannot pause while {self.status.value}")
            self._unpaused.clear()
            if self.status == BotStatus.SCANNING:
                self.status = BotStatus.PAUSED
            return self.snapshot()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self.status != BotStatus.PAUSED:
                raise StateError(f"Cannot resume while {self.status.value}")
            self._unpaused.set()
            self.status = BotStatus.SCANNING
            return self.snapshot()

    def toggle(self, dry_run: bool = True, interval_seconds: float | None = None) -> dict[str, Any]:
        with self._lock:
            if self.status in (BotStatus.STOPPED, BotStatus.ERROR):
                return self.start(dry_run, interval_seconds)
            if self.status == BotStatus.SCANNING:
                return self.pause()
            if self.status == BotStatus.PAUSED:
                return self.resume()
            raise StateError(f"Cannot toggle while {self.status.value}")

    def stop(self, timeout: float = 30.0) -> dict[str, Any]:
        with self._lock:
            if self.status in (BotStatus.STOPPED, BotStatus.ERROR) and self._thread is None:
                self.status = BotStatus.STOPPED
                return self.snapshot()
            self.status = BotStatus.STOPPING
            self._stop.set()
            self._unpaused.set()  # let a paused worker see the stop
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("Scan thread did not stop within %.0fs; it will exit after its current pass", timeout)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
                self.status = BotStatus.STOPPED
            return self.snapshot()

    # ------------------------------------------------------------ queries
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status.value,
                "dry_run": self.config.dry_run,
                "interval_seconds": self.config.interval_seconds,
                "connected": self._runner is not None and self.status in (BotStatus.SCANNING, BotStatus.PAUSED),
                "started_at": _iso(self.started_at),
                "last_run_at": _iso(self.last_run_at),
                "runs": self.runs,
                "last_error": self.last_error,
                "account": self.account,
                "last_result": self.last_result,
                "risk": self.risk,
            }

    def signal_view(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status.value,
                "last_run_at": _iso(self.last_run_at),
                "last_result": self.last_result,
                "detectors": self.detectors,
            }

    def positions(self) -> dict[str, Any]:
        with self._lock:
            runner = self._runner if self.status in (BotStatus.SCANNING, BotStatus.PAUSED) else None
        if runner is None:
            raise NotConnectedError("Bot is not connected to MT5. Start it to query positions.")
        return runner.positions()

    def close_ticket(self, ticket: int) -> dict[str, Any]:
        with self._lock:
            runner = self._runner if self.status in (BotStatus.SCANNING, BotStatus.PAUSED) else None
        if runner is None:
            raise NotConnectedError("Bot is not connected to MT5. Start it to manage trades.")
        return runner.close_ticket(ticket)

    def wait_for_runs(self, count: int, timeout: float = 5.0) -> bool:
        """Block until at least ``count`` passes have completed (for tests and scripts)."""
        deadline = time.monotonic() + timeout
        with self._runs_changed:
            while self.runs < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.status == BotStatus.ERROR:
                    return self.runs >= count
                self._runs_changed.wait(remaining)
            return True

    def wait_for_status(self, status: BotStatus, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._runs_changed:
            while self.status != status:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._runs_changed.wait(remaining)
            return True

    # ------------------------------------------------------------ worker
    def _worker(self) -> None:
        with self._lock:
            config = self.config
        runner = None
        try:
            runner = self._runner_factory(config)
            account = runner.start()
        except Exception as exc:
            logger.exception("Bot failed to start")
            self._finish(BotStatus.ERROR, f"Start failed: {exc}", runner)
            return

        with self._lock:
            self._runner = runner
            self.account = account
            if self.status == BotStatus.STARTING:
                self.status = BotStatus.SCANNING if self._unpaused.is_set() else BotStatus.PAUSED
            self._runs_changed.notify_all()

        while not self._stop.is_set():
            if not self._unpaused.wait(timeout=0.2):
                continue
            if self._stop.is_set():
                break
            try:
                cycle = runner.run_once()
                with self._lock:
                    self.last_result = cycle["result"]
                    self.detectors = cycle["detectors"]
                    self.risk = cycle.get("risk")
                    self.account = cycle["account"]
                    self.last_error = None
            except Exception as exc:  # a failed pass is logged; the bot keeps scanning
                logger.exception("Scan pass failed")
                with self._lock:
                    self.last_error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self.runs += 1
                self.last_run_at = _now()
                self._runs_changed.notify_all()
            self._stop.wait(config.interval_seconds)

        self._finish(BotStatus.STOPPED, None, runner)

    def _finish(self, status: BotStatus, error: str | None, runner: Runner | None) -> None:
        if runner is not None:
            try:
                runner.close()
            except Exception:
                logger.exception("Error closing MT5 session")
        with self._lock:
            self._runner = None
            self.status = status
            if error:
                self.last_error = error
            if self._thread is threading.current_thread():
                self._thread = None
            self._runs_changed.notify_all()


# --------------------------------------------------------------------------- #
# Credentials (.env)
# --------------------------------------------------------------------------- #
CREDENTIAL_KEYS = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH")


class CredentialStore:
    """Read/update MT5 credentials in ``.env``. The password is never returned."""

    def __init__(self, env_path: Path = DEFAULT_ENV_PATH) -> None:
        self.env_path = Path(env_path)
        self._lock = threading.Lock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            values = _parse_env(self.env_path)
        login = values.get("MT5_LOGIN", "")
        password = values.get("MT5_PASSWORD", "")
        return {
            "login": int(login) if login.isdigit() else None,
            "server": values.get("MT5_SERVER") or None,
            "path": values.get("MT5_PATH") or None,
            "password": PASSWORD_MASK if password else "",
            "password_set": bool(password),
            "env_file": str(self.env_path),
        }

    def update(
        self,
        login: int | None,
        server: str | None,
        password: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Write credentials. ``password`` of None, "" or the mask keeps the stored one."""
        if login is not None and login <= 0:
            raise ValueError("login must be a positive account number")
        for name, value in (("server", server), ("path", path), ("password", password)):
            if value is not None and ("\n" in value or "\r" in value):
                raise ValueError(f"{name} must not contain line breaks")

        updates: dict[str, str] = {
            "MT5_LOGIN": str(login) if login is not None else "",
            "MT5_SERVER": (server or "").strip(),
        }
        if path is not None:
            updates["MT5_PATH"] = path.strip()
        if password not in (None, "", PASSWORD_MASK):
            updates["MT5_PASSWORD"] = password

        with self._lock:
            current = _parse_env(self.env_path)
            if updates["MT5_LOGIN"] and not (updates["MT5_SERVER"] and (
                    updates.get("MT5_PASSWORD") or current.get("MT5_PASSWORD"))):
                raise ValueError("server and password are required when login is set")
            _write_env(self.env_path, updates)
        # load_settings() never overrides variables already in the process
        # environment, so mirror the new values there too.
        for key, value in updates.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        return self.read()


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ENV_LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            values[m.group(1)] = _unquote(m.group(2))
    return values


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw.split(" #", 1)[0].strip()  # unquoted values may carry an inline comment


def _quote(value: str) -> str:
    if value == "" or re.fullmatch(r"[A-Za-z0-9_.:/\\-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Replace or append ``updates`` in ``path``, keeping other lines and comments."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        m = _ENV_LINE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            out.append(line)
    out.extend(f"{key}={_quote(value)}" for key, value in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # owner-only; no-op beyond read-only flag on Windows
    except OSError:
        pass
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses, timestamps and numpy scalars for JSON."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, enum.Enum):
        return obj.value
    return obj


def _position_dict(p: Any) -> dict[str, Any]:
    return {
        "ticket": p.ticket,
        "type": "BUY" if getattr(p, "type", 0) == 0 else "SELL",
        "volume": getattr(p, "volume", None),
        "price_open": getattr(p, "price_open", None),
        "price_current": getattr(p, "price_current", None),
        "sl": getattr(p, "sl", None),
        "tp": getattr(p, "tp", None),
        "profit": getattr(p, "profit", None),
        "swap": getattr(p, "swap", None),
        "time": _ts(getattr(p, "time", None)),
        "comment": getattr(p, "comment", ""),
    }


def _order_result_dict(result: OrderResult, action: str) -> dict[str, Any]:
    return {
        "action": action,
        "success": result.success,
        "retcode": result.retcode,
        "retcode_name": result.retcode_name,
        "message": result.message,
        "ticket": result.order,
        "deal": result.deal,
        "price": result.price,
    }


ORDER_TYPES = {2: "BUY_LIMIT", 3: "SELL_LIMIT", 4: "BUY_STOP", 5: "SELL_STOP"}


def _order_dict(o: Any) -> dict[str, Any]:
    return {
        "ticket": o.ticket,
        "type": ORDER_TYPES.get(getattr(o, "type", None), str(getattr(o, "type", ""))),
        "volume": getattr(o, "volume_current", getattr(o, "volume_initial", None)),
        "price_open": getattr(o, "price_open", None),
        "sl": getattr(o, "sl", None),
        "tp": getattr(o, "tp", None),
        "time_setup": _ts(getattr(o, "time_setup", None)),
        "comment": getattr(o, "comment", ""),
    }


def _ts(value: Any) -> str | None:
    if value in (None, 0):
        return None
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None
