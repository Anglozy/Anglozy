"""Faithful Python port of 'AURUM 15M ICT Mechanical v1' for backtesting on real XAUUSD 15m data.

Pine semantics replicated:
- process_orders_on_close=true -> entry fills at the CLOSE of the signal bar
- exits (TP1 50% at tp1R, TP2 at PDH/PDL, stop at sweep extreme +/- buffer) are live from the next bar;
  if stop and a target are touched in the same bar, the stop is assumed first (pessimistic)
- sessions in Africa/Nairobi (UTC+3, no DST): Asia 02-09, London 10-12, NY 16:30-19:00
- PDH/PDL = previous completed daily bar; DXY SMT approximated with inverted EURUSD
Costs: bar spread + slippage per side (same model as the rest of the research).
"""
import numpy as np, pandas as pd
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user-Anglozy/62cd4c4f-9157-579a-95a9-f26ceb74b159/scratchpad/bt")
import htf as H
from engine import rma

D = H.D


def load():
    g = pd.read_pickle(f"{D}/gold15.pkl")
    e = pd.read_pickle(f"{D}/eur15.pkl")
    e = e.reindex(g.index).ffill()
    return g, e


def run(g, e, p):
    P = dict(riskPct=1.0, dailyLossPct=3.0, maxTrades=4, minRR=2.0, tp1R=2.0, slBuffer=0.5, eqBand=3.5,
             dispMult=1.0, mssLen=5, useSMT=True, smtLen=20, slip=0.10, scale_by_price=False,
             longs=True, shorts=True, tp1_frac=0.5, be_after_tp1=False)
    P.update(p)
    idx = g.index; o, h, l, c = (g[k].values for k in ("open", "high", "low", "close")); spr = g.spread.values
    n = len(c)
    eat = idx + pd.Timedelta(hours=3)
    mins = np.asarray(eat.hour * 60 + eat.minute)
    inAsia = (mins >= 120) & (mins < 540)
    inLon = (mins >= 600) & (mins < 720)
    inNY = (mins >= 990) & (mins < 1140)
    inKZ = inLon | inNY
    pc = np.r_[np.nan, c[:-1]]
    atr = rma(np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0), 14)
    # previous completed daily bar (broker day, 22:00 UTC roll like OANDA's 17:00 NY)
    dd = H.resample(g, "1D")
    pdh = H.htf_series(g, dd, dd.high.values); pdl = H.htf_series(g, dd, dd.low.values)
    # DXY proxy: inverse EURUSD
    dxyH = 1.0 / e.low.values; dxyL = 1.0 / e.high.values
    hiPrev = pd.Series(dxyH).shift(1).rolling(P["smtLen"]).max().values
    loPrev = pd.Series(dxyL).shift(1).rolling(P["smtLen"]).min().values
    dxyNoNewHigh = dxyH <= hiPrev; dxyNoNewLow = dxyL >= loPrev
    # price-scaled buffers (optional fix): express $ amounts relative to 2024 gold ~ $2300
    scale = c / 2300.0 if P["scale_by_price"] else np.ones(n)
    day = np.asarray((idx + pd.Timedelta(hours=2)).date)  # TV "D" for OANDA ~ 17:00 NY roll
    trades = []
    asiaHi = asiaLo = np.nan
    bA = bF = sA = sF = False; bSL = bLo = bHi = sSL = sLo = sHi = np.nan; bSMT = sSMT = False
    tradesToday = 0; dayStartEq = 1.0; eq = 1.0; curDay = None
    pos = 0
    hh2 = pd.Series(h).shift(2).rolling(P["mssLen"]).max().values  # ta.highest(high[2], mssLen)
    ll2 = pd.Series(l).shift(2).rolling(P["mssLen"]).min().values
    for i in range(3, n):
        # ---- manage open position on this bar (orders placed on an earlier bar)
        if pos and i > ei:
            stop_hit = (l[i] <= sl) if pos > 0 else (h[i] >= sl)
            if stop_hit:
                px = sl if ((pos > 0 and o[i] > sl) or (pos < 0 and o[i] < sl)) else o[i]
                realized += q * (px - ent) * pos; q = 0
            else:
                if not tp1done and ((pos > 0 and h[i] >= tp1) or (pos < 0 and l[i] <= tp1)):
                    px = tp1 if ((pos > 0 and o[i] < tp1) or (pos < 0 and o[i] > tp1)) else o[i]
                    realized += P["tp1_frac"] * (px - ent) * pos; q -= P["tp1_frac"]; tp1done = True
                    if P["be_after_tp1"]:
                        sl = ent + pos * cost
                if q > 1e-9 and ((pos > 0 and h[i] >= tp2) or (pos < 0 and l[i] <= tp2)):
                    px = tp2 if ((pos > 0 and o[i] < tp2) or (pos < 0 and o[i] > tp2)) else o[i]
                    realized += q * (px - ent) * pos; q = 0
            if q <= 1e-9:
                R = (realized - cost) / risk
                trades.append(dict(entry_time=idx[ei], exit_time=idx[i], dir=pos, entry=ent, risk=risk, R=R,
                                   R_gross=realized / risk, cost_R=cost / risk, tp1=tp1done, bars=i - ei))
                eq *= 1 + R * P["riskPct"] / 100; pos = 0
        # ---- daily gates
        if day[i] != curDay:
            curDay = day[i]; tradesToday = 0; dayStartEq = eq
        dailyLossHit = (dayStartEq - eq) / dayStartEq * 100 >= P["dailyLossPct"]
        # ---- Asia range
        if inAsia[i] and not inAsia[i - 1]:
            asiaHi, asiaLo = h[i], l[i]
        elif inAsia[i]:
            asiaHi = max(asiaHi, h[i]); asiaLo = min(asiaLo, l[i])
        eqm = (asiaHi + asiaLo) / 2
        nearEq = abs(c[i] - eqm) <= P["eqBand"] * scale[i] if np.isfinite(eqm) else False
        refLo = pdl[i] if not np.isfinite(asiaLo) else min(asiaLo, pdl[i])
        refHi = pdh[i] if not np.isfinite(asiaHi) else max(asiaHi, pdh[i])
        canTrade = tradesToday < P["maxTrades"] and not dailyLossHit and not nearEq and pos == 0
        bullSweep = inKZ[i] and np.isfinite(refLo) and l[i] < refLo and c[i] > refLo
        bearSweep = inKZ[i] and np.isfinite(refHi) and h[i] > refHi and c[i] < refHi
        bullRetest = bA and bF and inKZ[i] and l[i] <= bHi and c[i] > bLo
        bearRetest = sA and sF and inKZ[i] and h[i] >= sLo and c[i] < sHi
        if bullRetest:
            bA = bF = False
            if canTrade and (not P["useSMT"] or bSMT) and P["longs"]:
                ent = c[i]; risk = ent - bSL; t1 = ent + P["tp1R"] * risk; t2 = pdh[i]
                if risk > 0 and (t2 - ent) >= max(P["minRR"], P["tp1R"]) * risk:
                    pos = 1; ei = i; sl = bSL; tp1 = t1; tp2 = t2; q = 1.0; realized = 0.0; tp1done = False
                    cost = spr[i] + 2 * P["slip"]; tradesToday += 1
        if bearRetest:
            sA = sF = False
            if canTrade and pos == 0 and (not P["useSMT"] or sSMT) and P["shorts"]:
                ent = c[i]; risk = sSL - ent; t1 = ent - P["tp1R"] * risk; t2 = pdl[i]
                if risk > 0 and (ent - t2) >= max(P["minRR"], P["tp1R"]) * risk:
                    pos = -1; ei = i; sl = sSL; tp1 = t1; tp2 = t2; q = 1.0; realized = 0.0; tp1done = False
                    cost = spr[i] + 2 * P["slip"]; tradesToday += 1
        # ---- state updates
        if bullSweep:
            bA = True; bF = False; bSL = l[i] - P["slBuffer"] * scale[i]; bSMT = bool(dxyNoNewHigh[i])
        if bearSweep:
            sA = True; sF = False; sSL = h[i] + P["slBuffer"] * scale[i]; sSMT = bool(dxyNoNewLow[i])
        bullDisp = c[i - 1] > o[i - 1] and (c[i - 1] - o[i - 1]) >= P["dispMult"] * atr[i - 1]
        bullMSS = c[i - 1] > hh2[i]; bullGap = l[i] > h[i - 2]
        if bA and not bF and inKZ[i] and bullDisp and bullMSS and bullGap:
            bF = True; bLo = h[i - 2]; bHi = l[i]
        bearDisp = c[i - 1] < o[i - 1] and (o[i - 1] - c[i - 1]) >= P["dispMult"] * atr[i - 1]
        bearMSS = c[i - 1] < ll2[i]; bearGap = h[i] < l[i - 2]
        if sA and not sF and inKZ[i] and bearDisp and bearMSS and bearGap:
            sF = True; sLo = h[i]; sHi = l[i - 2]
        if not inKZ[i]:
            bA = bF = sA = sF = False
    return pd.DataFrame(trades)


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    g, e = load()
    rows = []
    V = {"as written (SMT via inv-EURUSD)": {}, "no SMT filter": {"useSMT": False},
         "price-scaled $ buffers": {"scale_by_price": True}, "no SMT + scaled": {"useSMT": False, "scale_by_price": True},
         "longs only": {"shorts": False}, "BE after TP1": {"be_after_tp1": True}, "zero costs": {"slip": 0.0}}
    out = {}
    for k, v in V.items():
        t = run(g, e, v); out[k] = t
        for lab, x in [("full", t), ("2017-21", t[t.entry_time < "2022"] if len(t) else t), ("2022-26", t[t.entry_time >= "2022"] if len(t) else t)]:
            s = H.stats(x, risk_pct=1.0, label=f"{k} | {lab}")
            if len(x): s["gross"] = round(x.R_gross.mean(), 3); s["tp1%"] = round(x.tp1.mean() * 100, 1)
            rows.append(s)
    print(pd.DataFrame(rows).to_string())
    t = out["as written (SMT via inv-EURUSD)"]
    if len(t):
        print(pd.DataFrame([H.stats(ty, 1.0, str(y)) for y, ty in t.groupby(t.entry_time.dt.year)])[["label", "n", "win", "expR", "PF", "totR"]].to_string())
        t.to_pickle(f"{D}/aurum_trades.pkl")
