# Gold Trend Rider (4H) — research notes and setup

## What it trades

**XAUUSD on the 4-hour chart, longs only by default.**

| Part | Rule |
|---|---|
| Entry | The 4H candle closes above the highest high of the previous 55 candles |
| Trend filter 1 | The 4H close is above the 4H EMA(200) |
| Trend filter 2 | Yesterday's daily close is above the daily EMA(100) (previous completed day, so it doesn't repaint) |
| Fill | At the next candle's open |
| Initial stop | 2.0 × ATR(14) |
| Partial exit | 50% at +0.5R, then the stop moves to breakeven plus a $0.40 buffer for costs |
| Runner | Chandelier trailing stop: highest high since entry − 3.0 × ATR(14) |
| Size | Equity × risk% ÷ (stop distance × point value); default risk 0.5% |
| News | Blocks new entries if the fill candle contains US NFP, or a UTC time you list |

## How it was validated

* **Data:** XAUUSD 1-minute bars from a MetaTrader 5 broker, Apr 2017 – Aug 2026, with real spreads. FX bars came from MT5 as well. Both are public GitHub datasets, so they're broker data, not exchange data.
* **Costs:** the bar's own spread, plus $0.10 slippage per side, plus a 0.01%-per-night swap.
* **Fills:** like TradingView: signal on the close, fill at the next open. If the stop and target are both touched in the same bar, the stop is assumed to fill first.
* **Tuning:** parameters were chosen on **2017–2021 only**. A setting had to work on both gold and EURUSD, and the whole neighbourhood of settings had to be profitable (92–96% of the tested gold settings were).
* **Final test:** the rules were frozen, then tested **once** on 2022–2026. Three FX pairs were held back as unseen markets.

### Results (after costs, longs only)

| Period | Trades | Win rate | Avg win / loss | Avg R per trade | Profit factor | Max drawdown at 0.5% risk |
|---|---|---|---|---|---|---|
| Tuning, 2017–2021 | 72 | 73.6% | +0.69R / −1.03R | +0.235R | 1.87 | 2.3% |
| **Untouched, 2022–2026** | 101 | **72.3%** | +0.66R / −0.98R | **+0.207R** | 1.76 | 3.7% |
| Full period | 173 | 72.8% | +0.67R / −1.00R | +0.219R | 1.81 | 4.6% |

Yearly R per trade: 2017 +0.30, 2018 −0.07, 2019 +0.40, 2020 +0.65, 2021 −0.24, 2022 −0.03, 2023 0.00, 2024 +0.22, 2025 +0.45, 2026 (to Aug) +0.21.

### Stress tests

| Test | Result |
|---|---|
| Triple slippage | Still profitable: +0.10R on untouched years (long + short version) |
| 4H candles shifted 1h / 2h / 3h | +0.185 / +0.159 / +0.038R on untouched years. Positive at every alignment, but the size of the edge depends on where the candles start |
| Random entries with the same filters and exits (200 runs) | Random entries also win about 68% of trades, so **the high win rate comes from the exit design**. The breakout entry adds roughly +0.1R per trade over random entries: p = 0.025 over the full period, p = 0.07 for longs on the untouched years |
| Monte Carlo drawdown (5,000 reshuffles) | At 0.5% risk: median 3.1%, 95th percentile 5.4%. At 1%: 10.9%. At 2%: 20.3% |
| Shorts | Lost money in both periods (−0.03R tuning, −0.09R untouched), so they're off by default |
| EURUSD | Lost money on untouched years (−0.08R) and did no better than random. **Don't run this bot on FX.** |
| EURJPY / GBPJPY / CHFJPY (never used for tuning) | −0.00 / +0.04 / +0.07R: roughly breakeven |

### ICT-style filters tested on top (plan 3)

Each filter was judged on 2017–2021 first, with adopt-only-if-clearly-better as the rule. The untouched years were run once afterwards for information.

| Filter | 2017–21: trades / R per trade / total R | 2022–26: trades / R per trade / total R | Verdict |
|---|---|---|---|
| **None (current rules)** | 72 / +0.235 / 16.9 | 101 / +0.207 / 20.9 | **Kept** |
| Kill-zone timing (London / NY AM only) | 66 / +0.189 / 12.5 | 87 / +0.156 / 13.6 | Worse in both periods |
| Skip Asian-session signals | 65 / +0.250 / 16.3 | 96 / +0.194 / 18.6 | No real change; optional switch, off |
| Recent sweep of a 10-bar swing low | 16 / +0.325 / 5.2 | 21 / +0.212 / 4.4 | Cuts trades 78%, total profit much lower; optional switch, off |
| Recent sweep of the previous-day low | 16 / +0.087 / 1.4 | 20 / +0.472 / 9.4 | Results flip between periods (noise); rejected |
| Breakout candle closes in the upper half | 70 / +0.238 / 16.7 | 102 / +0.194 / 19.7 | No change |

### What to expect

* **About 19 trades a year** (1–2 a month), holding about 2 days on average.
* **Returns scale with risk.** On the untouched years: 0.5% risk gave about 2.3% a year with a 3.7% max drawdown; 1% gave about 4.6% a year (7.2% drawdown); 2% gave about 9.1% a year (14% drawdown).
* **This is far below simply holding gold over 2022–2026**, which roughly doubled. The bot's value is low drawdown and being out of the market most of the time, not beating a gold bull run.
* **Most wins are small** (about +0.24R once the partial is taken and the runner stops out at breakeven). A few big runners (+2R to +5R) make the profit.
* **Flat or losing years happen** (2018, 2021–2023). The longest losing streak was 5 trades.
* **Gold's 2017–2026 uptrend helped.** In a long gold bear market this long-only system would mostly sit out, and could lose.

### What was tried and rejected

| Idea (source paper) | Result on real data |
|---|---|
| VWAP + EMA50 pullback on 15m (gold paper) | −0.12R per trade, losing before costs in every variant. The paper's own "backtest" (sec. 6.1) simulated assumed win/loss probabilities, not real prices |
| First half-hour → last half-hour momentum (FX momentum paper) | About 0.1 bp edge, below the spread |
| London opening-range breakout (USTEC paper style) | Profitable on tuning years, failed on untouched years |
| RSI(2) dip-buying in a higher-timeframe trend, 1H/4H | 60–70% win rate but about zero expectancy |
| Same breakout on the 1H chart | Lost money after costs |

## On-chart panel

TradingView's *Profitable trades* % counts the partial exit and the runner as two separate trades, which inflates it (for example 80.8% on 2023–2026). The script draws a small panel in the top-right corner that counts **whole positions**. Compare that number with the research figure (about 72%).

## AURUM 15M (demo only)

`aurum_15m_demo.pine` is the user-supplied ICT strategy with its bugs fixed:
* working webhook alerts (JSON `alert_message` on every order, plus `alert()` notifications)
* sizing that respects your quantity step and skips a trade rather than forcing 1 unit
* no same-bar long-and-short
* the same built-in costs as Gold Trend Rider
* an optional breakeven after TP1

The 9-year backtest (`research/aurum.py`) shows 42 trades, 43% win rate, −0.01R per trade overall: it lost in 2017–21 (−0.40R) and won in 2022–26 (+0.29R). **Forward-test it on demo only.** Run it on the XAUUSD **15m** chart.

## TradingView setup

1. Open **XAUUSD** on the **4H** chart. Your feed (OANDA, FOREX.com, Pepperstone…) will differ a little from the test data.
2. Pine Editor → paste `gold_trend_rider.pine` → *Add to chart*.
3. **Costs are built into the script.** It charges $0.25 per ounce on entry and again on exit ($0.50 round trip), which covers spread plus slippage. You don't need to change Slippage or Commission in *Settings → Properties*. Just set *Initial capital* to your account size. The one exception: if your feed's contract is 100 oz rather than 1 oz, set Commission to 25 USD per contract.
4. **Settings → Inputs → Quantity step**: set it to your broker's minimum size, in the units TradingView uses for the symbol (usually ounces for CFD feeds). Check `syminfo.pointvalue` with your broker.
5. **News:** keep *Auto-block NFP* on, and add CPI/FOMC times in UTC to the event list (`2026-12-16 19:00`).
6. **Webhook alerts:** *Create Alert* → Condition: **Gold Trend Rider 4H** → **Order fills only** → Webhook URL from your bridge (PineConnector, TradersPost, 3Commas…) → Message: `{{strategy.order.alert_message}}`. The JSON payloads are:
   * entry: `{"action":"buy","symbol":"XAUUSD","qty":…,"sl":…,"tp1":…}`. The levels are estimates from the signal close; the fill is at the next open.
   * partial: `{"action":"close_partial","percent":50}`
   * exit: `{"action":"close_all"}`

   Edit them to match your bridge's exact syntax. Also place the `sl` at the broker as a hard protective stop.
7. **Demo trade it first.** Run it on a demo account for at least 2–3 months, about 5–10 trades, and compare fills with the TradingView *List of Trades* before using real money.

## Reproducing the research

The Python research code is in `research/`. `trend.py` is the model the Pine Script mirrors, and `oos.py` / `bench.py` are the final tests. The data paths point at the session's download folder, so they'd need re-pointing to run elsewhere.
