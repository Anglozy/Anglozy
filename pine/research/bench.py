import pandas as pd, numpy as np, trend as T, htf as H
from multiprocessing import Pool
F=dict(tf="4h",n=55,sl_k=2.0,trail_k=3.0,ltf_ema=200,d_ema=100,use_daily=True,part_r=0.5,part_q=0.5,be=True)
def job(a):
    m,st,side,seed=a
    kw=dict(F,start=st); 
    if side=="L": kw["shorts"]=False
    t=T.run(m,rand=(seed,0.03),**kw); return (m,side,t.R.mean(),len(t),(t.R>0).mean())
if __name__=="__main__":
    cases=[("GOLD","2022-01-01","both"),("GOLD","2022-01-01","L"),("GOLD",None,"both"),("EURUSD","2018-01-01","both")]
    real={}
    for m,st,side in cases:
        kw=dict(F,start=st); 
        if side=="L": kw["shorts"]=False
        t=T.run(m,**kw); real[(m,st,side)]=t.R.mean()
    with Pool(4) as p:
        res=p.map(job,[(m,st,side,s) for m,st,side in cases for s in range(200)])
    r=pd.DataFrame(res,columns=["m","side","exp","n","win"])
    i=0
    for m,st,side in cases:
        x=r.iloc[i*200:(i+1)*200]; i+=1
        print(f"{m} start={st} side={side}: strategy expR={real[(m,st,side)]:+.3f} | random-entry mean={x.exp.mean():+.3f} 95th pct={x.exp.quantile(.95):+.3f}  p(rand>=strategy)={(x.exp>=real[(m,st,side)]).mean():.3f}  rand n~{x.n.mean():.0f} win~{x.win.mean()*100:.0f}%")
