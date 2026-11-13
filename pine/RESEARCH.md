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

## TradingView setup

1. Open **XAUUSD** on the **4H** chart. Your feed (OANDA, FOREX.com, Pepperstone…) will differ a little from the test data.
2. Pine Editor → paste `gold_trend_rider.pine` → *Add to chart*.
3. Strategy **Settings → Properties**:
   * Initial capital: your account size.
   * **Slippage**: enter ticks equal to about $0.30, which covers half the spread plus slippage. On a 0.01 tick feed that's 30 ticks; on a 0.001 tick feed, 300.
   * Leave *Recalculate on every tick* off.
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
