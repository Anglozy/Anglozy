"""Bar-by-bar backtester mirroring TradingView strategy() semantics.

- Signals evaluated on bar close; orders fill at the NEXT bar's open
  (Pine default, process_orders_on_close=false).
- Stops/targets checked intrabar against high/low; if both are inside the
  same bar the STOP is assumed first (pessimistic).
- Costs: per-trade spread (from data) + slippage on each side, in price.
- Returns are in R (multiples of initial risk), net of costs.
"""
import numpy as np
import pandas as pd

DEFAULT = dict(
    ema_reg=200, ema_pull=50, ema_tight=20, atr_len=14, vol_len=20,
    boundary=0.001,           # no trades when |C-EMA200|/EMA200 < 0.1%
    use_vwap=True,
    vwap_tz="UTC",            # "UTC" = paper (13:30-20:00 UTC fixed); "NY" = 09:30-16:00 ET
    sess_start="13:30", sess_end="20:00",
    trade_start=None, trade_end=None,  # entry window (defaults = session)
    pin=True, engulf=True, pin_wick=2.0, pin_opp=0.5,
    vol_mult=1.1, rng_mult=0.8,
    sl_atr=0.5, tp_r=3.0, tighten_r=2.5,
    trail=True,               # close beyond EMA50 -> exit next open
    flat_at_end=True,         # flatten at session end
    max_losses_day=3,
    news_et=(),               # list of (hh, mm) ET; block entries +-news_win minutes
    news_win=15,
    slip=0.0,                 # per side, price units
    longs=True, shorts=True,
    min_risk_spreads=0.0,     # skip if risk < k * spread
    htf=None,                 # (rule, length): regime from higher-TF EMA of last COMPLETED HTF bar
    htf_slope=False,          # also require HTF EMA rising (long) / falling (short)
    use_e200=True,            # keep the 15m EMA200 regime filter
    time_stop=0,              # bars; exit at next open after N bars (0 = off)
)


def rma(x, n):
    a = 1.0 / n
    out = np.empty_like(x); out[:] = np.nan
    s = np.nanmean(x[:n]); out[n - 1] = s
    for i in range(n, len(x)):
        s = a * x[i] + (1 - a) * s; out[i] = s
    return out


def ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().values


def indicators(df, p):
    o, h, l, c, v = (df[k].values.astype(float) for k in ("open", "high", "low", "close", "volume"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    ind = dict(o=o, h=h, l=l, c=c, v=v,
               e_reg=ema(c, p["ema_reg"]), e_pull=ema(c, p["ema_pull"]), e_t=ema(c, p["ema_tight"]),
               atr=rma(tr, p["atr_len"]),
               vma=pd.Series(v).rolling(p["vol_len"]).mean().values,
               spr=df["spread"].values.astype(float))
    # session / VWAP
    idx = df.index
    if p["vwap_tz"] == "NY":
        loc = idx.tz_localize("UTC").tz_convert("America/New_York")
    else:
        loc = idx
    mins = loc.hour * 60 + loc.minute
    sh, sm = map(int, p["sess_start"].split(":")); eh, em = map(int, p["sess_end"].split(":"))
    s0, s1 = sh * 60 + sm, eh * 60 + em
    in_sess = (mins >= s0) & (mins < s1)
    day = np.asarray(loc.date)
    ts = p["trade_start"] or p["sess_start"]; te = p["trade_end"] or p["sess_end"]
    t0 = int(ts[:2]) * 60 + int(ts[3:]); t1 = int(te[:2]) * 60 + int(te[3:])
    in_trade = (mins >= t0) & (mins < t1)
    tp = (h + l + c) / 3
    vw = np.full(len(c), np.nan); cpv = cv = 0.0; prev_day = None; active = False
    for i in range(len(c)):
        if in_sess[i]:
            if not active or day[i] != prev_day:
                cpv = cv = 0.0; active = True; prev_day = day[i]
            cpv += tp[i] * v[i]; cv += v[i]
            vw[i] = cpv / cv if cv > 0 else tp[i]
        else:
            active = False
    # last bar of session: next bar is outside session (or new day)
    last_sess = in_sess & ~np.r_[in_sess[1:], False]
    # news blackout (ET clock): bars that overlap [t-win, t+win]
    ny = idx.tz_localize("UTC").tz_convert("America/New_York")
    nym = ny.hour * 60 + ny.minute
    block = np.zeros(len(c), bool)
    for hh, mm in p["news_et"]:
        t = hh * 60 + mm
        # signal bar close time = nym+15; entry occurs at that time
        ent = nym + 15
        block |= (ent >= t - p["news_win"]) & (ent <= t + p["news_win"])
    ind["htf"] = np.full(len(c), np.nan); ind["htf_up"] = np.zeros(len(c), bool); ind["htf_dn"] = np.zeros(len(c), bool)
    if p["htf"]:
        rule, ln = p["htf"]
        hc = df["close"].resample(rule, label="left", closed="left").last().dropna()
        he = hc.ewm(span=ln, adjust=False).mean()
        prev = he.shift(1)
        # value known at bar i = EMA as of last fully completed HTF bar => shift HTF series by one, align by HTF bucket
        bucket = df.index.floor(rule) if rule.endswith("min") or rule.endswith("h") else df.index.normalize()
        ind["htf"] = he.shift(1).reindex(bucket).values
        ind["htf_up"] = (he.shift(1) > he.shift(2)).reindex(bucket).values.astype(bool)
        ind["htf_dn"] = (he.shift(1) < he.shift(2)).reindex(bucket).values.astype(bool)
        ind["htf_c"] = hc.shift(1).reindex(bucket).values
    ind.update(vw=vw, in_sess=in_sess, in_trade=in_trade, last_sess=last_sess, day=day, block=block)
    return ind


def signals(ind, p):
    o, h, l, c, v = ind["o"], ind["h"], ind["l"], ind["c"], ind["v"]
    o1, c1, l1, h1 = (np.r_[np.nan, x[:-1]] for x in (o, c, l, h))
    e50, e200, atr = ind["e_pull"], ind["e_reg"], ind["atr"]
    body = np.abs(c - o)
    lw = np.minimum(o, c) - l; uw = h - np.maximum(o, c)
    rng = h - l
    far = np.abs(c - e200) / e200 >= p["boundary"]
    volok = v > p["vol_mult"] * ind["vma"] if p["vol_mult"] > 0 else np.ones(len(c), bool)
    rngok = rng >= p["rng_mult"] * atr
    vw = ind["vw"]
    vwl = (c > vw) if p["use_vwap"] else np.ones(len(c), bool)
    vws = (c < vw) if p["use_vwap"] else np.ones(len(c), bool)
    pin_l = (lw >= p["pin_wick"] * body) & (uw <= p["pin_opp"] * lw) & p["pin"]
    pin_s = (uw >= p["pin_wick"] * body) & (lw <= p["pin_opp"] * uw) & p["pin"]
    eng_l = (c > o) & (c1 < o1) & (c > o1) & (o < c1) & p["engulf"]
    eng_s = (c < o) & (c1 > o1) & (c < o1) & (o > c1) & p["engulf"]
    base = far & volok & rngok & ind["in_trade"] & ~ind["block"]
    if p["use_vwap"]:
        base &= ~np.isnan(vw)
    rl = (c > e200) if p["use_e200"] else np.ones(len(c), bool)
    rs = (c < e200) if p["use_e200"] else np.ones(len(c), bool)
    if not p["use_e200"]:
        far = np.ones(len(c), bool); base = volok & rngok & ind["in_trade"] & ~ind["block"]
        if p["use_vwap"]: base &= ~np.isnan(vw)
    if p["htf"]:
        hv = ind["htf"]
        rl = rl & (c > hv); rs = rs & (c < hv)
        if p["htf_slope"]:
            rl = rl & ind["htf_up"]; rs = rs & ind["htf_dn"]
    L = base & rl & vwl & (np.minimum(l, l1) <= e50) & (e50 <= c) & (pin_l | eng_l) & p["longs"]
    S = base & rs & vws & (np.maximum(h, h1) >= e50) & (e50 >= c) & (pin_s | eng_s) & p["shorts"]
    return np.nan_to_num(L).astype(bool), np.nan_to_num(S).astype(bool)


def backtest(df, params=None, return_ind=False):
    p = dict(DEFAULT); p.update(params or {})
    ind = indicators(df, p)
    L, S = signals(ind, p)
    o, h, l, c = ind["o"], ind["h"], ind["l"], ind["c"]
    e50, e20, atr, spr = ind["e_pull"], ind["e_t"], ind["atr"], ind["spr"]
    n = len(c); idx = df.index
    trades = []
    pos = 0; entry = sl = tp = risk = 0.0; ei = -1; cost = 0.0
    pending = 0; pend_sl = 0.0; exit_next = False; tight = False
    losses_day = {}; cur_day = None
    for i in range(max(p["ema_reg"], 50), n):
        d = ind["day"][i]
        # ---- fills at open of bar i
        if pos != 0 and exit_next:
            px = o[i]
            trades.append(_close(idx, ei, i, pos, entry, px, risk, cost, "trail/flat"))
            _book(trades[-1], losses_day); pos = 0; exit_next = False
        if pending != 0 and pos == 0:
            entry = o[i]; sl = pend_sl
            risk = (entry - sl) if pending > 0 else (sl - entry)
            cost = spr[i - 1] + 2 * p["slip"]
            if risk > max(1e-9, p["min_risk_spreads"] * spr[i - 1]):
                pos = pending; ei = i; tight = False
                tp = entry + pos * p["tp_r"] * risk
            pending = 0
        pending = 0
        # ---- intrabar stop / target
        if pos != 0:
            hit_sl = (l[i] <= sl) if pos > 0 else (h[i] >= sl)
            hit_tp = (h[i] >= tp) if pos > 0 else (l[i] <= tp)
            if hit_sl:
                px = sl if ((pos > 0 and o[i] > sl) or (pos < 0 and o[i] < sl)) else o[i]  # gap through
                trades.append(_close(idx, ei, i, pos, entry, px, risk, cost, "stop"))
                _book(trades[-1], losses_day); pos = 0
            elif hit_tp:
                px = tp if ((pos > 0 and o[i] < tp) or (pos < 0 and o[i] > tp)) else o[i]
                trades.append(_close(idx, ei, i, pos, entry, px, risk, cost, "target"))
                _book(trades[-1], losses_day); pos = 0
        # ---- close-of-bar management
        if pos != 0:
            fl = (c[i] - entry) * pos / risk
            if fl >= p["tighten_r"]:
                tight = True
            trail = e20[i] if tight else e50[i]
            if p["trail"] and i > ei - 1 and ((pos > 0 and c[i] < trail) or (pos < 0 and c[i] > trail)):
                exit_next = True
            if p["time_stop"] and i - ei + 1 >= p["time_stop"]:
                exit_next = True
            if p["flat_at_end"] and ind["last_sess"][i]:
                exit_next = True
        # ---- new signal
        if pos == 0 and not exit_next:
            if losses_day.get(d, 0) >= p["max_losses_day"]:
                continue
            if ind["last_sess"][i] and p["flat_at_end"]:
                continue
            if L[i]:
                pending = 1; pend_sl = l[i] - p["sl_atr"] * atr[i]
            elif S[i]:
                pending = -1; pend_sl = h[i] + p["sl_atr"] * atr[i]
    t = pd.DataFrame(trades)
    if return_ind:
        return t, ind, (L, S)
    return t


def _close(idx, ei, xi, pos, entry, px, risk, cost, why):
    gross = (px - entry) * pos
    return dict(entry_time=idx[ei], exit_time=idx[xi], dir=pos, entry=entry, exit=px,
                risk=risk, R=(gross - cost) / risk, R_gross=gross / risk, cost_R=cost / risk, why=why,
                day=idx[ei].date())


def _book(t, losses_day):
    if t["R"] < 0:
        losses_day[t["day"]] = losses_day.get(t["day"], 0) + 1


def stats(t, risk_pct=0.5, label=""):
    if t is None or len(t) == 0:
        return dict(label=label, n=0)
    R = t["R"].values
    eq = np.cumprod(1 + R * risk_pct / 100)
    dd = 1 - eq / np.maximum.accumulate(eq)
    yrs = max((t["exit_time"].iloc[-1] - t["entry_time"].iloc[0]).days / 365.25, 0.1)
    wins = R[R > 0].sum(); loss = -R[R < 0].sum()
    m = t.set_index("exit_time")["R"].resample("ME").sum()
    sharpe = m.mean() / m.std() * np.sqrt(12) if m.std() > 0 else np.nan
    return dict(label=label, n=len(R), per_yr=round(len(R) / yrs, 1), win=round((R > 0).mean() * 100, 1),
                expR=round(R.mean(), 3), PF=round(wins / loss, 2) if loss > 0 else np.inf,
                totR=round(R.sum(), 1), CAGR=round((eq[-1] ** (1 / yrs) - 1) * 100, 2),
                maxDD=round(dd.max() * 100, 1), sharpe_m=round(sharpe, 2),
                costR=round(t["cost_R"].mean(), 3))
