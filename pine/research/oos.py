import pandas as pd, numpy as np, trend as T, htf as H, strats as S
pd.set_option("display.width",260)
L="/tmp/claude-0/-home-user-Anglozy/62cd4c4f-9157-579a-95a9-f26ceb74b159/scratchpad/data/lxloby_15-minute-quick-win-strategy/data/cache/"
import subprocess
FROZEN=dict(tf="4h",n=55,sl_k=2.0,trail_k=3.0,ltf_ema=200,d_ema=100,use_daily=True,part_r=0.5,part_q=0.5,be=True)
ALT=dict(FROZEN,part_r=0.75)
def jpy(sym):
    d=pd.read_parquet(L+f"{sym}_M15.parquet"); d.index=pd.to_datetime(d.time_utc); d=d.rename(columns={"tick_volume":"volume"})
    d["spread"]=(d.spread*0.001).clip(upper=0.05); d=d[d.index>="2010-01-01"][["open","high","low","close","volume","spread"]]
    return d
rows=[]
def rep(label,t):
    s=H.stats(t,label=label); s["gross"]=round(t.R_gross.mean(),3); rows.append(s); return t
OOS={"GOLD":"2022-01-01","EURUSD":"2018-01-01"}
for m,st in OOS.items():
    t=rep(f"{m} OOS frozen",T.run(m,start=st,**{k:v for k,v in FROZEN.items()})); t.to_pickle(f"tr_oos_{m}.pkl")
    rep(f"{m} OOS alt pr0.75",T.run(m,start=st,**ALT))
    rep(f"{m} OOS cost x3",T.run(m,start=st,slip_mult=3,**FROZEN))
    rep(f"{m} OOS longs only",T.run(m,start=st,shorts=False,**FROZEN))
    rep(f"{m} OOS shorts only",T.run(m,start=st,longs=False,**FROZEN))
    rep(f"{m} FULL period",T.run(m,**FROZEN)).to_pickle(f"tr_full_{m}.pkl")
for sym in ["EURJPY","GBPJPY","CHFJPY"]:
    b=jpy(sym); df=H.resample(b,"4h"); dd=H.resample(b,"1D")
    t=rep(f"{sym} UNSEEN 2010-26",T.run(sym,df=df,dd=dd,cost=dict(slip=0.002,swap=0.00003),**{k:v for k,v in FROZEN.items() if k!="tf"}))
    t.to_pickle(f"tr_{sym}.pkl")
print(pd.DataFrame(rows).to_string())
