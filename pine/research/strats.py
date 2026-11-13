import numpy as np, pandas as pd
import htf as H
from engine import ema

_cache = {}


def bars(mkt, tf):
    k = (mkt, tf)
    if k not in _cache:
        base = pd.read_pickle(f"{H.D}/{H.MKT[mkt]['f']}")
        _cache[k] = H.resample(base, tf) if tf != "15min" else base
    return _cache[k]


def trend_dip(mkt, tf="1h", rsi_len=2, th=10, sl_k=3.0, max_bars=10, d_ema=50, ltf_ema=200,
              exit_mode="sma5", tp_k=0.0, longs=True, shorts=True, start=None, end=None, confirm=False):
    """Buy short-term oversold dips inside a higher-timeframe uptrend (and mirror for shorts)."""
    df = bars(mkt, tf); dd = bars(mkt, "1D")
    c = df.close.values; o = df.open.values
    r = H.rsi(c, rsi_len); a = H.atr(df.high.values, df.low.values, c, 14)
    e_l = ema(c, ltf_ema) if ltf_ema else np.full(len(c), -np.inf)
    dc = H.htf_series(df, dd, dd.close.values); de = H.htf_series(df, dd, ema(dd.close.values, d_ema))
    up = (dc > de) & (c > e_l); dn = (dc < de) & (c < (e_l if ltf_ema else np.inf))
    le = up & (r < th) & longs; se = dn & (r > 100 - th) & shorts
    if confirm:  # require the signal bar to close off its low (rejection) -> fewer falling-knife entries
        h, l = df.high.values, df.low.values
        le &= (c - l) >= 0.5 * (h - l); se &= (h - c) >= 0.5 * (h - l)
    if exit_mode == "sma5":
        s5 = pd.Series(c).rolling(5).mean().values; lx = c > s5; sx = c < s5
    elif exit_mode == "rsi50":
        lx = r > 50; sx = r < 50
    elif exit_mode == "rsi70":
        lx = r > 70; sx = r < 30
    else:
        lx = sx = np.zeros(len(c), bool)
    sl = sl_k * a; tp = tp_k * a if tp_k else None
    m = np.ones(len(c), bool)
    if start: m &= df.index >= start
    if end: m &= df.index < end
    t = H.simulate(df, le & m, se & m, lx, sx, sl, tp, max_bars, H.MKT[mkt]["slip"], H.MKT[mkt]["swap"])
    return t


def donchian(mkt, tf="4h", n=55, sl_k=2.0, trail_k=3.0, ltf_ema=200, start=None, end=None, longs=True, shorts=True):
    df = bars(mkt, tf); c = df.close.values
    hh = pd.Series(df.high.values).rolling(n).max().shift(1).values
    ll = pd.Series(df.low.values).rolling(n).min().shift(1).values
    a = H.atr(df.high.values, df.low.values, c, 14); e = ema(c, ltf_ema)
    le = (c > hh) & (c > e) & longs; se = (c < ll) & (c < e) & shorts
    m = np.ones(len(c), bool)
    if start: m &= df.index >= start
    if end: m &= df.index < end
    return H.simulate(df, le & m, se & m, None, None, sl_k * a, None, 0, H.MKT[mkt]["slip"], H.MKT[mkt]["swap"],
                      trail_dist=trail_k * a)


def random_bench(t, mkt, tf, seed=0, reps=200):
    """Same number of trades, same direction mix, same holding periods, random start times
    (within the same period). Returns distribution of mean gross R-equivalent using the same SL/exit
    is complex; we use mean price return over the same holding length, normalised by each trade's risk."""
    df = bars(mkt, tf); c = df.close.values; o = df.open.values
    rng = np.random.default_rng(seed)
    pos0 = df.index.searchsorted(t.entry_time.min()); pos1 = df.index.searchsorted(t.exit_time.max())
    hold = t.bars.clip(lower=1).values; d = t.dir.values; risk = t.risk.values
    out = []
    for _ in range(reps):
        s = rng.integers(pos0, pos1 - hold.max() - 1, len(hold))
        g = (o[np.minimum(s + hold, len(o) - 1)] - o[s]) * d / risk
        out.append(g.mean())
    return np.array(out)
