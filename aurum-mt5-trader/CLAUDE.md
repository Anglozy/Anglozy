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
- **Jinja2** — all HTML output is rendered from templates in `templates/`. Never build
  HTML with string concatenation or f-strings in Python code.
- **pytest** — test runner.

## Project layout

```
aurum-mt5-trader/
├── config/          # settings.py (terminal credentials, trading settings) and strategy parameters
├── src/
│   ├── aurum/       # Jinja2 HTML report generator and CSS styles
│   ├── mt5/         # MT5 terminal connection, order placement, market data fetcher
│   └── signals/     # Fair Value Gap, Order Block, liquidity sweep detection
├── templates/       # Aurum HTML output templates (*.html.j2)
├── tests/           # pytest suites
└── reports/         # generated Aurum HTML files (git-ignored output)
```

## Strict modular design

Each package has one responsibility and a narrow, explicit dependency direction:

```
config ──▶ mt5 ──▶ signals ──▶ aurum
```

- **`src/mt5`** is the *only* package allowed to `import MetaTrader5`. Everything else
  receives plain Python data (dataclasses, lists, or pandas DataFrames) — never raw
  MT5 objects.
- **`src/signals`** contains pure functions: OHLC data in, detected signals out. No I/O,
  no MT5 calls, no network, no clock reads. This keeps detection logic deterministic
  and fully unit-testable.
- **`src/aurum`** only renders data it is given. It must not fetch market data or
  place orders.
- **`config`** holds settings and parameters only — no logic beyond validation.
- No circular imports. No package may import from a package to its right in the
  diagram above except through data it is handed.
- Keep modules small and focused: one concept per module (e.g. `signals/fvg.py`,
  `signals/order_block.py`, `signals/liquidity_sweep.py`).
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
- New signal logic or order logic is not done until it has tests.

## Commands

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (required for live MT5)
pip install -r requirements.txt
cp .env.example .env            # then fill in credentials
pytest                          # run the test suite
python src/aurum/generator.py   # generate a sample XAUUSD report
```

Scripts importing `config` or `src/` packages run from the project root with
`PYTHONPATH=.;src` (Windows) or `PYTHONPATH=.:src`; pytest sets this automatically.
