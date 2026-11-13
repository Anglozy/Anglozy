"""Plan 3: do ICT-style liquidity-sweep / kill-zone filters improve Gold Trend Rider?
Pre-registered variants, judged on 2017-2021 only; survivors get ONE out-of-sample run."""
import sys; sys.path.insert(0,"/tmp/claude-0/-home-user-Anglozy/62cd4c4f-9157-579a-95a9-f26ceb74b159/scratchpad/bt")
import numpy as np, pandas as pd, htf as H, strats as S
from engine import ema
pd.set_option("display.width",250)
F=dict(n=55,sl_k=2.0,trail_k=3.0,ltf_ema=200,d_ema=100,part_r=0.5,part_q=0.5)

def run(mkt="GOLD", filt=None, start=None, end=None, df=None, dd=None):
    df=S.bars(mkt,"4h") if df is None else df; dd=S.bars(mkt,"1D") if dd is None else dd
    o,h,l,c=(df[k].values for k in ("open","high","low","close")); spr=df.spread.values; idx=df.index
    hh=pd.Series(h).rolling(F["n"]).max().shift(1).values
    a=H.atr(h,l,c,14); e=ema(c,F["ltf_ema"])
    dc=H.htf_series(df,dd,dd.close.values); de=H.htf_series(df,dd,ema(dd.close.values,F["d_ema"]))
    pdl=H.htf_series(df,dd,dd.low.values); pdh=H.htf_series(df,dd,dd.high.values)
    le=(c>hh)&(c>e)&(dc>de)
    hr=np.asarray(idx.hour)  # 4H bar OPEN hour UTC: 0,4,8,12,16,20 ; signal at close = open+4
    closeh=(hr+4)%24
    rng=h-l; body_top=(c-l)/np.where(rng>0,rng,np.nan)
    if filt=="kz_close":      # signal bar closes inside/at end of London or NY AM kill zone (UTC 08..16 close)
        le&=np.isin(closeh,[8,12,16])
    elif filt=="kz_contains":  # signal bar overlaps London (07-09 UTC) or NY AM (13:30-16 UTC)
        le&=np.isin(hr,[4,8,12])
    elif filt=="no_asia":      # skip signals formed in Asia hours (bar opening 20,0 UTC)
        le&=~np.isin(hr,[20,0])
    elif filt=="sweep_pdl_6":  # bullish sweep of previous-day low (wick below PDL, close back above) within last 6 bars (24h)
        sw=(l<pdl)&(c>pdl); le&=pd.Series(sw).rolling(6,min_periods=1).max().values.astype(bool)
    elif filt=="sweep_swing_10": # sweep of a 10-bar swing low then reclaim within last 10 bars
        sl10=pd.Series(l).rolling(10).min().shift(1).values; sw=(l<sl10)&(c>sl10)
        le&=pd.Series(sw).rolling(10,min_periods=1).max().values.astype(bool)
    elif filt=="no_rejection":  # breakout candle closes in upper 50% of its range (no buy-side sweep-and-reject)
        le&=body_top>=0.5
    elif filt=="not_sweep_high": # skip if breakout bar is a sweep of PDH that closes back... (close below PDH)
        le&=~((h>pdh)&(c<pdh))
    m=np.ones(len(c),bool)
    if start: m&=idx>=start
    if end: m&=idx<end
    slip=H.MKT[mkt]["slip"]; swap=H.MKT[mkt]["swap"]
    tr=[]; pos=0; pend=False
    for i in range(1,len(c)):
        if pend and not pos:
            ent=o[i]; risk=F["sl_k"]*a[i-1]; sl=ent-risk; pos=1; ei=i; q=1.0; real=0.0; part=False; cst=spr[i-1]+2*slip; best=ent
        pend=False
        if pos:
            if l[i]<=sl:
                px=sl if o[i]>sl else o[i]; real+=q*(px-ent); tr.append((idx[ei],idx[i],ent,risk,real,cst,swap)); pos=0
            else:
                tg=ent+F["part_r"]*risk
                if not part and h[i]>=tg:
                    px=tg if o[i]<tg else o[i]; real+=F["part_q"]*(px-ent); q-=F["part_q"]; part=True; sl=ent+cst
        if pos:
            best=max(best,h[i]); sl=max(sl,best-F["trail_k"]*a[i])
        elif m[i] and le[i]: pend=True
    t=pd.DataFrame(tr,columns=["entry_time","exit_time","entry","risk","real","cst","swap"])
    nights=(t.exit_time.dt.normalize()-t.entry_time.dt.normalize()).dt.days.clip(lower=0)
    t["R"]=(t.real-t.cst-t.swap*t.entry*nights)/t.risk; t["R_gross"]=t.real/t.risk
    return t

if __name__=="__main__":
    V=[None,"kz_close","kz_contains","no_asia","sweep_pdl_6","sweep_swing_10","no_rejection","not_sweep_high"]
    mode=sys.argv[1] if len(sys.argv)>1 else "IS"
    rows=[]
    for v in V:
        if mode=="IS": t=run(filt=v,end="2022-01-01")
        else: t=run(filt=v,start="2022-01-01")
        s=H.stats(t,0.5,str(v)); rows.append(s)
    print(mode); print(pd.DataFrame(rows)[["label","n","per_yr","win","avgW","avgL","expR","PF","totR","maxDD","sharpe","maxLossStreak"]].to_string())
