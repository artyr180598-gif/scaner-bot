"""Frozen RID_Depth_Protocol implementation. Research only; no bot imports."""
import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT=Path('rid-depth-data')
SYMBOLS=['DOGEUSDT','WIFUSDT','1000PEPEUSDT']
HORIZON=360

def roll(x,n,fn):
    out=np.full(len(x),np.nan)
    out[n-1:]=fn(np.lib.stride_tricks.sliding_window_view(x,n),axis=1)
    return out

def candles(symbol):
    rows=[]
    for p in sorted((ROOT/symbol).glob('*-1m-*.zip')):
        with zipfile.ZipFile(p) as z:
            for r in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))):
                if r[0].isdigit():
                    rows.append([float(r[i]) for i in (0,1,2,3,4,7,10)])
    a=np.array(rows)
    assert len(a)>0 and np.all(np.diff(a[:,0])>0)
    assert np.all(np.isfinite(a)) and np.all(a[:,1:5]>0)
    assert np.all(a[:,2]>=np.maximum(a[:,1],a[:,4]))
    assert np.all(a[:,3]<=np.minimum(a[:,1],a[:,4]))
    assert np.all((a[:,6]>=0)&(a[:,6]<=a[:,5]*1.000001))
    return a

def depth(symbol):
    pairs={}
    for p in sorted((ROOT/symbol).glob('*-bookDepth-*.zip')):
        with zipfile.ZipFile(p) as z:
            for r in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))):
                if r[0]=='timestamp' or float(r[1]) not in (-.2,.2):
                    continue
                pair=pairs.setdefault(r[0],[None,None])
                side=0 if float(r[1])<0 else 1
                assert pair[side] is None, 'duplicate snapshot side'
                pair[side]=float(r[3])
    rows=[]
    bad=0
    for ts,(bid,ask) in sorted(pairs.items()):
        if bid is None or ask is None or not np.isfinite(bid+ask) or min(bid,ask)<0 or bid+ask<=0:
            bad+=1
            continue
        stamp=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()*1000
        rows.append([stamp,bid/(bid+ask)])
    d=np.array(rows)
    assert len(d)>0 and np.all(np.diff(d[:,0])>0)
    return d,bad

def align(a,d):
    # Candle close boundary + assumed availability delay, no future snapshot.
    boundary=a[:,0]+60000
    idx=np.searchsorted(d[:,0],boundary-5000,side='right')-1
    clipped=np.maximum(idx,0)
    age=boundary-d[clipped,0]
    valid=(idx>=0)&(age>=5000)&(age<=90000)
    share=np.where(valid,d[clipped,1],np.nan)
    return share

def signals(a,share):
    _,_,hi,lo,c,v,buy=a.T
    meanv=np.roll(roll(v,60,np.mean),1)
    high=np.roll(roll(hi,20,np.max),1)
    low=np.roll(roll(lo,20,np.min),1)
    ret=c/np.roll(c,60)-1
    ret5=c/np.roll(c,5)-1
    flow=roll(buy,5,np.sum)/np.maximum(roll(v,5,np.sum),1e-20)
    valid=np.isfinite(roll(share,5,np.min))
    dp=(roll(share,5,np.min)>=.60,roll(share,5,np.max)<=.40)
    fp=(flow>=.55,flow<=.45)
    bp=((c>high)&(ret>=.005)&(v>=1.5*meanv),
        (c<low)&(ret<=-.005)&(v>=1.5*meanv))
    masks={}
    for name in ['price_breakout','price_breakout_flow','price_breakout_depth',
                 'price_breakout_both','depth_only','absorption_proxy']:
        sides=[]
        for n in range(2):
            if name=='depth_only':
                m=dp[n]&fp[n]
            elif name=='absorption_proxy':
                m=dp[n]&fp[1-n]&(abs(ret5)<=.002)
            else:
                m=bp[n].copy()
                if name in ('price_breakout_flow','price_breakout_both'):
                    m &= fp[n]
                if name in ('price_breakout_depth','price_breakout_both'):
                    m &= dp[n]
            sides.append(m&valid)
        masks[name]=sides
    return masks,dp

def simulate(a,start,side,sl):
    entry=a[start,1]
    stop=entry*(1-side*sl)
    target=entry*(1+side*.01)
    funding=0.
    for k in range(start,start+HORIZON):
        _,op,hi,lo,close,_,_=a[k]
        funding+=op/entry*.0001/(8*60)
        hitstop=lo<=stop if side==1 else hi>=stop
        hittarget=hi>=target if side==1 else lo<=target
        if hitstop:
            exitprice=min(op,stop) if side==1 else max(op,stop)
            why='stop'
        elif hittarget:
            exitprice=target
            why='target'
        elif k==start+HORIZON-1:
            exitprice=close
            why='timeout'
        else:
            continue
        ratio=exitprice/entry
        gross=side*(ratio-1)
        return {'net':gross-.0006*(1+ratio)-funding,
                'stress_net':gross-.0012*(1+ratio)-funding,
                'minutes':k-start+1,'reason':why}

def summary(rows):
    if not rows:
        return {'n':0}
    out={'n':len(rows),'median_minutes':float(np.median([r['minutes'] for r in rows])),
         'timeout_pct':100*sum(r['reason']=='timeout' for r in rows)/len(rows)}
    for field in ['net','stress_net']:
        x=np.array([r[field] for r in rows])
        days={}
        for r in rows:
            days.setdefault(r['date'],[]).append(r[field])
        sums=np.array([sum(v) for v in days.values()])
        counts=np.array([len(v) for v in days.values()])
        idx=np.random.default_rng(20260905).integers(0,len(sums),(2000,len(sums)))
        boot=sums[idx].sum(axis=1)/counts[idx].sum(axis=1)
        neg=-x[x<0].sum()
        out[field]={'mean_pct':float(x.mean()*100),'win_pct':float(np.mean(x>0)*100),
                    'pf':float(x[x>0].sum()/neg) if neg else None,
                    'ci95_pct':[float(v*100) for v in np.quantile(boot,[.025,.975])],
                    'worst_pct':float(x.min()*100)}
    return out

def main():
    groups={}
    quality={}
    for symbol in SYMBOLS:
        a=candles(symbol)
        d,bad=depth(symbol)
        share=align(a,d)
        masks,dp=signals(a,share)
        quality[symbol]={'candles':len(a),'depth_pairs':len(d),'invalid_pairs':bad,
                         'stale_or_missing_minutes':int(np.sum(~np.isfinite(share))),
                         'candle_gaps':int(np.sum(np.diff(a[:,0])!=60000))}
        cache={}
        for name,(long,short) in masks.items():
            for sl in (.01,.03):
                key=f'{name}/sl{sl}'
                groups.setdefault(key,[])
                if name=='price_breakout':
                    groups.setdefault(key+'/depth_accept',[])
                    groups.setdefault(key+'/depth_reject',[])
                last=-HORIZON
                for i in np.flatnonzero(long|short):
                    start=int(i)+1
                    if i<65 or start+HORIZON>len(a) or start<last+HORIZON:
                        continue
                    if a[start+HORIZON-1,0]-a[i-60,0]!=(HORIZON+60)*60000:
                        continue
                    ts=datetime.fromtimestamp(a[start,0]/1000,timezone.utc)
                    end=datetime.fromtimestamp(a[start+HORIZON-1,0]/1000,timezone.utc)
                    if ts.month!=end.month:
                        continue
                    side=1 if long[i] else -1
                    last=start
                    ck=(start,side,sl)
                    if ck not in cache:
                        cache[ck]=simulate(a,start,side,sl)
                    r=dict(cache[ck],symbol=symbol,date=ts.date().isoformat(),
                           time_ms=int(a[start,0]),month=ts.month,side=side)
                    groups[key].append(r)
                    if name=='price_breakout':
                        flag=dp[0 if side==1 else 1][i]
                        groups[key+('/depth_accept' if flag else '/depth_reject')].append(r)
        print('processed',symbol,quality[symbol],flush=True)
    result={'quality':quality,'results':{},'records':groups,'promotion_gate':{}}
    for name,rows in groups.items():
        result['results'][name]={str(m):summary([r for r in rows if r['month']==m]) for m in (7,8)}
        result['results'][name]['august_symbols']={s:summary([r for r in rows if r['month']==8 and r['symbol']==s]) for s in SYMBOLS}
        if name.endswith('/sl0.01'):
            j=result['results'][name]['7']; au=result['results'][name]['8']
            goodcoins=sum(v['n']>=20 and v.get('stress_net',{}).get('mean_pct',-1)>0 for v in result['results'][name]['august_symbols'].values())
            passed=(j['n']>0 and au['n']>=100 and j['stress_net']['mean_pct']>0
                    and au['stress_net']['mean_pct']>0
                    and (au['stress_net']['pf'] is None or au['stress_net']['pf']>=1.2)
                    and au['stress_net']['ci95_pct'][0]>0 and goodcoins>=2)
            result['promotion_gate'][name]=bool(passed)
    Path('rid_depth_results.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({'results':{k:{m:v[m] for m in ('7','8')} for k,v in result['results'].items()},
                      'promotion_gate':result['promotion_gate']},indent=2))

if __name__=='__main__':
    main()
