import pandas as pd, numpy as np, engine as E, brk as B, sys
pd.set_option("display.width",250)
G=pd.read_pickle("gold15.pkl"); U=pd.read_pickle("eur15.pkl")
SLIP={"GOLD":0.10,"EURUSD":0.00002}
BASE=dict(clock="UTC",r_start="06:00",r_end="08:00",e_end="12:00",x_time="20:00",tp_r=3,min_rng_atr=0,trend=20,be_r=1.0)
def nfp_days(idx):
    d=pd.Series(pd.to_datetime(sorted(set(idx.date))))
    return set(str(x.date()) for x in d if x.weekday()==4 and x.day<=7)
def run(mkt,df,var):
    p=dict(BASE,slip=SLIP[mkt]); p.update(var); return B.run(df,p)
if __name__=="__main__":
    mode=sys.argv[1]
    if mode=="IS":
        D={"GOLD":G[G.index<"2022-01-01"],"EURUSD":U[U.index<"2018-01-01"]}
        V={"base":{},"trend10":{"trend":10},"trend30":{"trend":30},"trend50":{"trend":50},"no_trend":{"trend":None},
           "r05-08":{"r_start":"05:00"},"r06-0830":{"r_end":"08:30"},"e_end11":{"e_end":"11:00"},"e_end13":{"e_end":"13:00"},
           "x19":{"x_time":"19:00"},"x21":{"x_time":"21:00"},"tp2.5":{"tp_r":2.5},"tp4":{"tp_r":4},"be0.75":{"be_r":0.75},"be1.5":{"be_r":1.5},
           "buf0.1atr":{"buf_atr":0.1},"oco_off":{"oco":False},"skipNFP":"NFP","slipx2":"SLIP2"}
    else:
        D={"GOLD":G[G.index>="2022-01-01"],"EURUSD":U[U.index>="2018-01-01"]}
        V={"base":{},"skipNFP":"NFP","slipx2":"SLIP2","no_trend":{"trend":None}}
    rows=[]
    for m,df in D.items():
        for k,v in V.items():
            if v=="NFP": v={"skip_dates":nfp_days(df.index)}
            if v=="SLIP2": v={"slip":SLIP[m]*3}
            t=run(m,df,v); s=E.stats(t,label=k); s["mkt"]=m; s["grossR"]=round(t.R_gross.mean(),3); rows.append(s)
            if k=="base": t.to_pickle(f"t_{mode}_{m}.pkl")
    print(pd.DataFrame(rows)[["mkt","label","n","per_yr","win","grossR","costR","expR","PF","totR","CAGR","maxDD","sharpe_m"]].to_string())
