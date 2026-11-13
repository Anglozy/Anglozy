import pandas as pd, numpy as np, itertools, trend as T, htf as H, strats as S
from multiprocessing import Pool
pd.set_option("display.width",260)
END={"GOLD":"2022-01-01","EURUSD":"2018-01-01"}
grid=list(itertools.product(["4h","1h"],[20,55],[1.5,2.0,3.0],[3.0,4.0],[0,0.5,0.75,1.0,1.5],[100,200]))
def job(a):
    m,(tf,n,sl,trl,pr,de)=a
    t=T.run(m,tf,n,sl,trl,200,de,True,pr,0.5,True,end=END[m])
    s=H.stats(t); s.update(mkt=m,tf=tf,N=n,sl=sl,trl=trl,pr=pr,de=de); return s
if __name__=="__main__":
    for m in END:
        for tf in ["1h","4h","1D"]: S.bars(m,tf)
    with Pool(4) as p: res=p.map(job,[(m,g) for m in END for g in grid],chunksize=4)
    df=pd.DataFrame(res); df.to_pickle("g6.pkl")
    print(df.groupby(["mkt","tf","pr"]).agg(med_exp=("expR","median"),frac_pos=("expR",lambda x:(x>0).mean()),med_win=("win","median"),med_sh=("sharpe","median"),med_n=("n","median")).to_string())
    k=["tf","N","sl","trl","pr","de"]
    w=df.pivot_table(index=k,columns="mkt",values=["expR","win","sharpe","n","maxDD"],aggfunc="first")
    w.columns=[f"{a}_{b[:3]}" for a,b in w.columns]; w=w.reset_index(); w["minSh"]=w[["sharpe_GOL","sharpe_EUR"]].min(axis=1); w["minWin"]=w[["win_GOL","win_EUR"]].min(axis=1)
    print(w[w.minWin>=55].sort_values("minSh",ascending=False).head(20).to_string())
