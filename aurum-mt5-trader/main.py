"""Aurum MT5 Trader entry point.

Connects to MetaTrader 5, fetches closed XAUUSD bars, runs the ICT signal
pipeline, writes an Aurum HTML report to reports/ and places the limit order.

    python main.py                    # one pass, places the order if a setup exists
    python main.py --dry-run          # report only, no order
    python main.py --interval 60      # re-run every 60 seconds until Ctrl+C

Credentials and trading settings come from environment variables / .env
(see .env.example). Real-money accounts are refused unless
AURUM_ALLOW_LIVE_TRADING=true.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from aurum.generator import AurumReportGenerator  # noqa: E402
from config.settings import load_settings  # noqa: E402
from config.strategy import StrategyParameters  # noqa: E402
from mt5.connector import MT5Connector, MT5Error  # noqa: E402
from mt5.executor import MT5Executor  # noqa: E402
from signals.parser import PipelineResult, SignalPipeline  # noqa: E402

logger = logging.getLogger("aurum")

EXIT_OK = 0
EXIT_CONNECTION = 1
EXIT_ORDER_FAILED = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aurum MT5 Trader: ICT XAUUSD signal pipeline")
    parser.add_argument("--symbol", help="symbol to trade (default: AURUM_SYMBOL or XAUUSD)")
    parser.add_argument("--dry-run", action="store_true", help="generate the report but do not send orders")
    parser.add_argument("--interval", type=float, default=0,
                        help="seconds between runs; 0 runs once (default)")
    parser.add_argument("--utc-offset", type=int, default=None,
                        help="broker server time offset from UTC in hours (default: estimate from last tick)")
    parser.add_argument("--reports-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def run(args: argparse.Namespace, mt5_module: Any | None = None) -> int:
    """Run the pipeline once or on an interval. ``mt5_module`` lets tests inject a fake."""
    settings = load_settings()
    params = StrategyParameters(server_utc_offset_hours=args.utc_offset)
    symbol = args.symbol or settings.trading.symbol

    connector = MT5Connector(settings.credentials, settings.trading, mt5_module=mt5_module)
    try:
        connector.connect()
    except MT5Error as exc:
        logger.error("Could not connect to MT5: %s", exc)
        return EXIT_CONNECTION

    pipeline = SignalPipeline(
        connector,
        MT5Executor(connector),
        AurumReportGenerator(output_dir=args.reports_dir),
        params,
        execute=not args.dry_run,
    )
    exit_code = EXIT_OK
    try:
        while True:
            try:
                result = pipeline.run(symbol)
                _print_result(symbol, result)
                exit_code = EXIT_ORDER_FAILED if result.status == "order_failed" else EXIT_OK
            except MT5Error as exc:
                logger.error("Pipeline run failed: %s", exc)
                exit_code = EXIT_CONNECTION
            if args.interval <= 0:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        connector.disconnect()
    return exit_code


def _print_result(symbol: str, result: PipelineResult) -> None:
    print(f"[{symbol}] {result.status.upper()}: {result.message}")
    if result.signal:
        s = result.signal
        tps = ", ".join(f"{t.price} ({t.rr:.2f}R)" for t in s.targets)
        print(f"  {s.direction} limit {s.entry}  SL {s.stop_loss}  TP {tps}")
    if result.lot_size is not None:
        print(f"  Lot size: {result.lot_size}")
    if result.report_path:
        print(f"  Report:   {result.report_path}")


def main(argv: list[str] | None = None, mt5_module: Any | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    return run(args, mt5_module)


if __name__ == "__main__":
    sys.exit(main())
