"""Trend Rider: HTF breakout with scale-out. Faithful to Pine strategy semantics."""
import numpy as np, pandas as pd, htf as H, strats as S
from engine import ema

def run(mkt, tf="4h", n=20, sl_k=2.0, trail_k=3.0, ltf_ema=200, d_ema=200, use_daily=True,
        part_r=1.0, part_q=0.5, be=True, start=None, end=None, longs=True, shorts=True, slip_mult=1.0, df=None, dd=None, cost=None, rand=None):
    if df is None: df=S.bars(mkt,tf); dd=S.bars(mkt,"1D")
    o,h,l,c=(df[k].values for k in ("open","high","low","close")); spr=df.spread.values; idx=df.index
    hh=pd.Series(h).rolling(n).max().shift(1).values; ll=pd.Series(l).rolling(n).min().shift(1).values
    a=H.atr(h,l,c,14); e=ema(c,ltf_ema)
    if use_daily:
        dc=H.htf_series(df,dd,dd.close.values); de=H.htf_series(df,dd,ema(dd.close.values,d_ema))
        dup=dc>de; ddn=dc<de
    else: dup=ddn=np.ones(len(c),bool)
    le=(c>hh)&(c>e)&dup&longs; se=(c<ll)&(c<e)&ddn&shorts
    if rand is not None:  # (seed, prob): random entries that pass the same trend filters (no breakout)
        r=np.random.default_rng(rand[0]).random(len(c))<rand[1]
        le=(c>e)&dup&longs&r; se=(c<e)&ddn&shorts&r
    m=np.ones(len(c),bool)
    if start: m&=idx>=start
    if end: m&=idx<end
    slip=(cost or H.MKT[mkt])["slip"]*slip_mult; swap=(cost or H.MKT[mkt])["swap"]
    tr=[]; pos=0; pend=0
    for i in range(1,len(c)):
        if pend and not pos:
            ent=o[i]; risk=sl_k*a[i-1]; sl=ent-pend*risk; pos=pend; ei=i; q=1.0; realized=0.0; part_done=False
            cst=spr[i-1]+2*slip; best=ent
        pend=0
        if pos:
            # 1) stop (pessimistic first)
            if (pos>0 and l[i]<=sl) or (pos<0 and h[i]>=sl):
                px=sl if ((pos>0 and o[i]>sl) or (pos<0 and o[i]<sl)) else o[i]
                realized+=q*(px-ent)*pos; tr.append(_rec(idx,ei,i,pos,ent,risk,realized,cst,swap,"stop",part_done)); pos=0
            else:
                tgt=ent+pos*part_r*risk
                if part_r>0 and not part_done and ((pos>0 and h[i]>=tgt) or (pos<0 and l[i]<=tgt)):
                    px=tgt if ((pos>0 and o[i]<tgt) or (pos<0 and o[i]>tgt)) else o[i]
                    realized+=part_q*(px-ent)*pos; q-=part_q; part_done=True
                    if be: sl = ent + pos*cst  # breakeven incl. costs
                    if q<=1e-9:
                        tr.append(_rec(idx,ei,i,pos,ent,risk,realized,cst,swap,"target",True)); pos=0
        if pos:
            if pos>0: best=max(best,h[i]); sl=max(sl,best-trail_k*a[i])
            else: best=min(best,l[i]); sl=min(sl,best+trail_k*a[i])
        elif m[i]:
            if le[i]: pend=1
            elif se[i]: pend=-1
    return pd.DataFrame(tr)

def _rec(idx,ei,xi,pos,ent,risk,realized,cst,swap,why,part):
    nights=max(0,(idx[xi].normalize()-idx[ei].normalize()).days); sw=swap*ent*nights
    return dict(entry_time=idx[ei],exit_time=idx[xi],dir=pos,entry=ent,risk=risk,R=(realized-cst-sw)/risk,
                R_gross=realized/risk,cost_R=(cst+sw)/risk,why=why,partial=part,bars=xi-ei)
