"""Session range-breakout backtester (stop orders), Pine-equivalent semantics.

Each day (in a chosen clock: UTC or New York):
  range = high/low of bars in [r_start, r_end)
  from r_end until e_end: buy-stop at range high (+buf), sell-stop at range low (-buf)
  stop orders fill intrabar at max(level, open) (gap-through fills at open)
  SL: opposite side of range ("range"), or mid ("mid"), or k*ATR
  TP: tp_r * risk (0 = none); flatten at x_time (next bar open after the bar that starts x_time... we close at open of x_time bar)
  one trade per day per side; optionally 'oco' (first fill cancels other)
Same-bar conflict: SL checked before TP; on fill bar, SL is checked against the remaining bar
(pessimistic: if the fill bar's range also touches SL, it is a loss).
"""
import numpy as np, pandas as pd
from engine import rma, ema

DEF = dict(clock="UTC", r_start="00:00", r_end="07:00", e_end="12:00", x_time="20:00",
           sl="range", sl_atr=1.0, tp_r=0.0, buf_atr=0.0, oco=True, slip=0.0,
           min_rng_atr=0.0, max_rng_atr=99.0, trend=None, longs=True, shorts=True,
           be_r=0.0, weekdays=(0, 1, 2, 3, 4), skip_dates=())


def _mins(s):
    return int(s[:2]) * 60 + int(s[3:])


def run(df, prm=None):
    p = dict(DEF); p.update(prm or {})
    idx = df.index
    loc = idx.tz_localize("UTC").tz_convert("America/New_York") if p["clock"] == "NY" else idx
    m = np.asarray(loc.hour * 60 + loc.minute); day = np.asarray(loc.date); wd = np.asarray(loc.weekday)
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close")); spr = df.spread.values
    pc = np.r_[np.nan, c[:-1]]
    atr = rma(np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0), 14)
    # daily ATR proxy: 14-day RMA of daily ranges (completed days)
    dr = pd.DataFrame({"h": h, "l": l}, index=pd.Index(day)).groupby(level=0).agg({"h": "max", "l": "min"})
    datr = (dr.h - dr.l).ewm(alpha=1 / 14, adjust=False).mean().shift(1)
    trendv = None
    if p["trend"]:
        ln = p["trend"]
        dc = pd.Series(c, index=pd.Index(day)).groupby(level=0).last()
        de = dc.ewm(span=ln, adjust=False).mean()
        trendv = pd.DataFrame({"c": dc.shift(1), "e": de.shift(1)})
    rs, re_, ee, xt = map(_mins, (p["r_start"], p["r_end"], p["e_end"], p["x_time"]))
    trades = []
    # group bar indices per day
    starts = np.r_[0, np.nonzero(day[1:] != day[:-1])[0] + 1, len(day)]
    for a, b in zip(starts[:-1], starts[1:]):
        d = day[a]
        if wd[a] not in p["weekdays"] or str(d) in p["skip_dates"]:
            continue
        mm = m[a:b]
        rmask = (mm >= rs) & (mm < re_)
        if rmask.sum() < 2:
            continue
        ri = np.nonzero(rmask)[0] + a
        hi, lo = h[ri].max(), l[ri].min(); rng = hi - lo
        da = datr.get(d, np.nan)
        if not np.isfinite(da) or rng <= 0:
            continue
        if rng < p["min_rng_atr"] * da or rng > p["max_rng_atr"] * da:
            continue
        allow_l, allow_s = p["longs"], p["shorts"]
        if trendv is not None:
            tv = trendv.loc[d] if d in trendv.index else None
            if tv is None or not np.isfinite(tv.e):
                continue
            allow_l &= tv.c > tv.e; allow_s &= tv.c < tv.e
        buf = p["buf_atr"] * atr[ri[-1]]
        bl, bs = hi + buf, lo - buf
        pos = 0; done_l = not allow_l; done_s = not allow_s
        ent = sl = tp = risk = 0.0; cost = 0.0; ei = -1; be_done = False
        for i in range(ri[-1] + 1, b):
            if m[i] >= xt:  # flatten at open of x_time bar
                if pos:
                    trades.append(_t(idx, ei, i, pos, ent, o[i], risk, cost, "time", d)); pos = 0
                break
            # entries (only while in entry window)
            if pos == 0 and m[i] < ee and not (done_l and done_s):
                fill = 0
                up = (not done_l) and h[i] >= bl; dn = (not done_s) and l[i] <= bs
                if up and dn:   # both touched in one bar: ambiguous -> take the one nearer the open, pessimistic below
                    fill = 1 if abs(o[i] - bl) <= abs(o[i] - bs) else -1
                elif up: fill = 1
                elif dn: fill = -1
                if fill:
                    ent = max(bl, o[i]) if fill > 0 else min(bs, o[i])
                    if p["sl"] == "range": sl = lo - buf if fill > 0 else hi + buf
                    elif p["sl"] == "mid": sl = (hi + lo) / 2
                    else: sl = ent - fill * p["sl_atr"] * atr[i - 1]
                    risk = (ent - sl) * fill
                    if risk <= 0:
                        done_l |= fill > 0; done_s |= fill < 0; continue
                    tp = ent + fill * p["tp_r"] * risk if p["tp_r"] > 0 else np.nan
                    pos = fill; ei = i; cost = spr[i] + 2 * p["slip"]; be_done = False
                    if fill > 0: done_l = True
                    else: done_s = True
                    if p["oco"]: done_l = done_s = True
                    # same-bar stop check (pessimistic)
                    if (pos > 0 and l[i] <= sl) or (pos < 0 and h[i] >= sl):
                        trades.append(_t(idx, ei, i, pos, ent, sl, risk, cost, "stop", d)); pos = 0
                    continue
            if pos:
                if (pos > 0 and l[i] <= sl) or (pos < 0 and h[i] >= sl):
                    px = sl if ((pos > 0 and o[i] > sl) or (pos < 0 and o[i] < sl)) else o[i]
                    trades.append(_t(idx, ei, i, pos, ent, px, risk, cost, "stop", d)); pos = 0; continue
                if np.isfinite(tp) and ((pos > 0 and h[i] >= tp) or (pos < 0 and l[i] <= tp)):
                    px = tp if ((pos > 0 and o[i] < tp) or (pos < 0 and o[i] > tp)) else o[i]
                    trades.append(_t(idx, ei, i, pos, ent, px, risk, cost, "target", d)); pos = 0; continue
                if p["be_r"] > 0 and not be_done and (c[i] - ent) * pos >= p["be_r"] * risk:
                    sl = ent; be_done = True
        else:
            if pos:
                trades.append(_t(idx, ei, b - 1, pos, ent, c[b - 1], risk, cost, "eod", d))
    return pd.DataFrame(trades)


def _t(idx, ei, xi, pos, ent, px, risk, cost, why, d):
    g = (px - ent) * pos
    return dict(entry_time=idx[ei], exit_time=idx[xi], dir=pos, entry=ent, exit=px, risk=risk,
                R=(g - cost) / risk, R_gross=g / risk, cost_R=cost / risk, why=why, day=d)
