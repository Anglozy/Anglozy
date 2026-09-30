# CLAUDE.md — Aurum MT5 Trader

Guidelines for Claude (and humans) working in this project.

## Overview

Aurum MT5 Trader is a Python trading system for MetaTrader 5. It connects to an MT5
terminal, fetches market data, detects Smart Money Concepts (SMC) setups — Fair Value
Gaps, Order Blocks and liquidity sweeps — places orders, and renders "Aurum" HTML
reports with Jinja2.

## Tech stack

- **Python 3.10+** — use modern syntax: `X | Y` unions, `match`, built-in generics
  (`list[int]`), `dataclasses`. No compatibility shims for older versions.
- **MetaTrader5** (`import MetaTrader5 as mt5`) — the official MT5 Python API. It only
  runs on Windows with a local MT5 terminal installed.
- **Playwright** — headless Chromium for TradingView chart screenshots.
- **Jinja2** — all HTML output is rendered from templates in `templates/`. Never build
  HTML with string concatenation or f-strings in Python code.
- **pytest** — test runner.

## Project layout

```
aurum-mt5-trader/
├── main.py          # entry point: connect -> fetch bars -> pipeline -> report -> order
├── config/          # settings.py (credentials, trading settings), strategy.py (StrategyParameters)
├── src/
│   ├── aurum/       # Jinja2 report generator, CSS styles, TradingView chart capturer
│   ├── mt5/         # MT5 terminal connection, order placement, market data fetcher
│   └── signals/     # FVG, structure, Order Block, session sweep detectors + parser.py pipeline
├── templates/       # Aurum HTML output templates
├── tests/           # pytest suites (fake_mt5.py, scenarios.py hold shared fixtures)
└── reports/         # generated Aurum HTML files and images/ screenshots (git-ignored)
```

## Strict modular design

Each module has one responsibility and a narrow, explicit dependency direction:

```
config ──▶ signals detectors (pure)
config ──▶ mt5 (MetaTrader5 I/O)        aurum (rendering)
                  ╲                     ╱
                   ▶ signals/parser.py ◀        ◀── main.py
```

- **`src/mt5`** is the *only* package allowed to `import MetaTrader5`. Everything else
  receives plain Python data (dataclasses, lists, or pandas DataFrames) — never raw
  MT5 objects.
- **Signal detectors** (`signals/fvg.py`, `structure.py`, `order_block.py`,
  `liquidity.py`) are pure functions: OHLC DataFrame in, dataclasses out. No I/O, no
  MT5 calls, no network, no clock reads. They import only `config` and each other.
- **`signals/parser.py`** is the one orchestration module. `find_setup()` stays pure;
  `SignalPipeline` receives an `MT5Connector`, `MT5Executor` and
  `AurumReportGenerator` and wires them together. Keep new I/O here, not in detectors.
- **`src/aurum`** only renders data it is given. It must not fetch market data or
  place orders. `mt5` and `aurum` never import each other or `signals`. The one I/O
  exception is `aurum/capturer.py` (Playwright screenshots of TradingView charts),
  which only `SignalPipeline` calls; a capture failure must never block a trade.
- **MT5 bar times are broker server time**, not UTC. Session logic takes a
  `utc_offset_hours`; never compare bar times to UTC hours directly.
- **`config`** holds settings and parameters only — no logic beyond validation.
- No circular imports; imports only follow the arrows above.
- Keep modules small and focused: one concept per module.
- Public functions and classes get type hints and a short docstring.

## Configuration and secrets

- Never commit account numbers, passwords or server credentials. `config/settings.py`
  (`load_settings()`) reads them from environment variables (`MT5_LOGIN`, `MT5_PASSWORD`,
  `MT5_SERVER`, `MT5_PATH`) or a git-ignored `.env` file. See `.env.example` for all keys.
- Strategy parameters (symbol, timeframe, risk %, FVG/OB thresholds) live in `config/`,
  never hard-coded inside `src/`.

## Trading safety

- Default to a **demo account**. `MT5Connector` refuses real accounts unless
  `AURUM_ALLOW_LIVE_TRADING=true`.
- Every order must carry a stop-loss. Position size is derived from risk % and stop
  distance, never a fixed lot size.
- Check the `retcode` of every `order_send` result and log failures; never assume an
  order was filled.

## Testing

- Run tests from the project root: `pytest`
- Tests must not require a running MT5 terminal. Pass `tests/fake_mt5.FakeMT5` as
  `mt5_module=` to `MT5Connector` when testing `src/mt5`.
- Signal detectors are tested against small, hand-built OHLC fixtures with known answers.
- Tests never load TradingView. Mock the browser, or point `ChartCapturer(url_template=...)`
  at a local page (see `tests/test_capturer.py`); real-browser tests skip without Chromium.
- New signal logic or order logic is not done until it has tests.

## Commands

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (required for live MT5)
pip install -r requirements.txt
cp .env.example .env            # then fill in credentials
pytest                          # run the test suite
python src/aurum/generator.py   # generate a sample XAUUSD report
python main.py --dry-run        # run the live pipeline without sending orders
python main.py --interval 60    # run every 60s, placing orders (demo accounts only)
```

`main.py` adds `src/` to `sys.path` itself. Other scripts importing `config` or `src/`
packages need `PYTHONPATH=.;src` (Windows) or `PYTHONPATH=.:src`; pytest sets this
automatically.
