"""Higher-timeframe strategy research engine (TradingView strategy() semantics).

Signals on bar close -> fill at next bar open. SL/TP checked intrabar on later bars,
stop first if both touched (pessimistic). Exit signals on close -> fill next open.
Costs: spread + 2*slip per trade, plus overnight swap per night held (fraction of price).
"""
import numpy as np, pandas as pd
from engine import rma, ema

D = "/tmp/claude-0/-home-user-Anglozy/62cd4c4f-9157-579a-95a9-f26ceb74b159/scratchpad/bt"
MKT = {  # slip per side (price), swap per night as fraction of price
    "GOLD": dict(f="gold15.pkl", slip=0.10, swap=0.0001, split="2022-01-01"),
    "EURUSD": dict(f="eur15.pkl", slip=0.00002, swap=0.00003, split="2018-01-01"),
}


def resample(df, rule):
    if rule in ("1D", "D"):
        # trading day = 22:00 UTC roll (FX/metal convention, matches broker daily candles closely)
        x = df.copy(); x.index = x.index + pd.Timedelta(hours=2)
        out = x.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                    "volume": "sum", "spread": "median"}).dropna(subset=["open"])
        out = out[out.index.weekday < 5]
        out.index = out.index - pd.Timedelta(hours=2)
        return out
    return df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                  "volume": "sum", "spread": "median"}).dropna(subset=["open"])


def rsi(c, n):
    d = np.diff(c, prepend=np.nan)
    up = rma(np.where(d > 0, d, 0.0), n); dn = rma(np.where(d < 0, -d, 0.0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100 - 100 / (1 + up / dn)
    r[dn == 0] = 100
    return r


def atr(h, l, c, n=14):
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    return rma(tr, n)


def htf_series(df_ltf, df_htf, values):
    """Map a HTF series onto LTF bars using only COMPLETED HTF bars (no lookahead)."""
    s = pd.Series(values, index=df_htf.index)
    # an HTF bar with label t is complete at next HTF label; use shift(1) then asof
    s1 = s.shift(1)
    return s1.reindex(df_ltf.index, method="ffill").values


def simulate(df, le, se, lx=None, sx=None, sl_dist=None, tp_dist=None, max_bars=0,
             slip=0.0, swap=0.0, trail_dist=None):
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close")); spr = df.spread.values
    idx = df.index; n = len(c)
    lx = np.zeros(n, bool) if lx is None else lx; sx = np.zeros(n, bool) if sx is None else sx
    tr = []; pos = 0; pend = 0; xnext = False
    for i in range(1, n):
        if pos and xnext:
            tr.append(_t(idx, ei, i, pos, ent, o[i], risk, cost, swap, "signal")); pos = 0; xnext = False
        if pend and not pos:
            ent = o[i]; d = sl_dist[i - 1]
            sl = ent - pend * d; risk = d
            tp = ent + pend * tp_dist[i - 1] if tp_dist is not None else np.nan
            pos = pend; ei = i; cost = spr[i - 1] + 2 * slip; best = ent
        pend = 0
        if pos:
            if (pos > 0 and l[i] <= sl) or (pos < 0 and h[i] >= sl):
                px = sl if ((pos > 0 and o[i] > sl) or (pos < 0 and o[i] < sl)) else o[i]
                tr.append(_t(idx, ei, i, pos, ent, px, risk, cost, swap, "stop")); pos = 0
            elif np.isfinite(tp) and ((pos > 0 and h[i] >= tp) or (pos < 0 and l[i] <= tp)):
                px = tp if ((pos > 0 and o[i] < tp) or (pos < 0 and o[i] > tp)) else o[i]
                tr.append(_t(idx, ei, i, pos, ent, px, risk, cost, swap, "target")); pos = 0
        if pos:
            if trail_dist is not None:
                if pos > 0:
                    best = max(best, h[i]); sl = max(sl, best - trail_dist[i])
                else:
                    best = min(best, l[i]); sl = min(sl, best + trail_dist[i])
            if (pos > 0 and lx[i]) or (pos < 0 and sx[i]) or (max_bars and i - ei + 1 >= max_bars):
                xnext = True
        elif not xnext:
            if le[i] and np.isfinite(sl_dist[i]) and sl_dist[i] > 0: pend = 1
            elif se[i] and np.isfinite(sl_dist[i]) and sl_dist[i] > 0: pend = -1
        if not pos: xnext = False
    return pd.DataFrame(tr)


def _t(idx, ei, xi, pos, ent, px, risk, cost, swap, why):
    nights = max(0, (idx[xi].normalize() - idx[ei].normalize()).days)
    sw = swap * ent * nights
    g = (px - ent) * pos
    return dict(entry_time=idx[ei], exit_time=idx[xi], dir=pos, entry=ent, exit=px, risk=risk,
                R=(g - cost - sw) / risk, R_gross=g / risk, cost_R=(cost + sw) / risk, why=why, bars=xi - ei)


def stats(t, risk_pct=0.5, label=""):
    if t is None or len(t) == 0:
        return dict(label=label, n=0)
    R = t.R.values; eq = np.cumprod(1 + R * risk_pct / 100); dd = 1 - eq / np.maximum.accumulate(eq)
    yrs = max((t.exit_time.iloc[-1] - t.entry_time.iloc[0]).days / 365.25, 0.25)
    m = t.set_index("exit_time").R.resample("ME").sum()
    pf = R[R > 0].sum() / -R[R < 0].sum() if (R < 0).any() else np.inf
    return dict(label=label, n=len(R), per_yr=round(len(R) / yrs, 1), win=round((R > 0).mean() * 100, 1),
                avgW=round(R[R > 0].mean(), 2) if (R > 0).any() else 0, avgL=round(R[R < 0].mean(), 2) if (R < 0).any() else 0,
                expR=round(R.mean(), 3), PF=round(pf, 2), totR=round(R.sum(), 1),
                CAGR=round((eq[-1] ** (1 / yrs) - 1) * 100, 2), maxDD=round(dd.max() * 100, 1),
                sharpe=round(m.mean() / m.std() * np.sqrt(12), 2) if m.std() > 0 else np.nan,
                maxLossStreak=int(max_streak(R < 0)))


def max_streak(b):
    m = s = 0
    for x in b:
        s = s + 1 if x else 0; m = max(m, s)
    return m
