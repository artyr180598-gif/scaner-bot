"""Frozen exploratory test: July/August 2026, three coins, no tuning.

Compare the deployed-proposal detector with an RID-inspired (NOT RID-recovered)
depth+executed-flow confirmation. TP/SL 1%, 120m maximum, next minute after
signal close as entry (one minute delay), 6/12 bps per side costs, funding
proxy 1bp/8h. Independent per-model nonoverlap; no portfolio or martingale.
August was used in earlier studies, so is NOT an untouched holdout.
"""
import csv
import io
import json
import zipfile
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
from cryptopilot.models import Candle
from cryptopilot.impulse import detect_impulse
from rid_depth_study import depth, align, summary, SYMBOLS


def run():
    records = {'first_impulse': [], 'rid_depth_flow': []}
    for symbol in SYMBOLS:
        rows=[]
        for path in sorted((Path('rid-depth-data')/symbol).glob('*-1m-*.zip')):
            with zipfile.ZipFile(path) as z:
                for r in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))):
                    if r[0].isdigit():
                        rows.append([float(r[i]) for i in (0,1,2,3,4,5,7,10)])
        a=np.array(rows)
        assert np.all(np.diff(a[:,0]) == 60000)
        assert np.all(np.isfinite(a))
        d,_=depth(symbol)
        share=align(a,d)
        bars=[]; last_exit={name:-1 for name in records}
        for i in range(0,len(a)-4,5):
            block=a[i:i+5]
            assert block[0,0]%300000 == 0
            bars.append(Candle(int(block[0,0]),block[0,1],max(block[:,2]),
                               min(block[:,3]),block[-1,4],sum(block[:,5])))
            if len(bars)<49 or i+126>=len(a):
                continue
            boundary=int(block[-1,0]+60000)
            entry_index=i+6
            event=detect_impulse(bars[-49:],boundary+60000,a[entry_index,1])
            if event is None:
                continue
            direction=1 if event.direction=='LONG' else -1
            # Depth and executed taker flow must be available at signal close.
            shares=share[i:i+5]
            flow=sum(block[:,7])/max(sum(block[:,6]),1e-20)
            aligned=(np.isfinite(shares).all() and
                     ((min(shares)>=.6 and flow>=.55) if direction==1 else
                      (max(shares)<=.4 and flow<=.45)))
            for name in records:
                if entry_index<=last_exit[name] or (name=='rid_depth_flow' and not aligned):
                    continue
                start=a[entry_index,1]
                stop=start*(1-direction*.01); target=start*(1+direction*.01)
                for k in range(entry_index,entry_index+120):
                    op,hi,lo,close=a[k,1:5]
                    if (lo<=stop if direction==1 else hi>=stop):
                        exit_price=min(op,stop) if direction==1 else max(op,stop)
                        reason='stop'; break
                    if (hi>=target if direction==1 else lo<=target):
                        exit_price=target; reason='target'; break
                    exit_price=close; reason='timeout'
                last_exit[name]=k
                ratio=exit_price/start
                minutes=k-entry_index+1
                gross=direction*(ratio-1)
                funding=.0001*minutes/480
                date=datetime.fromtimestamp(a[entry_index,0]/1000,timezone.utc)
                records[name].append(dict(symbol=symbol,date=date.date().isoformat(),
                    time=date.isoformat(),month=date.month,
                    side=direction,entry=float(start),exit=float(exit_price),reason=reason,
                    minutes=minutes,net=float(gross-.0006*(1+ratio)-funding),
                    stress_net=float(gross-.0012*(1+ratio)-funding)))
        print(symbol, {n:len(r) for n,r in records.items()},flush=True)
    result={n:{'all':summary(r), 'july':summary([x for x in r if x['month']==7]),
               'august':summary([x for x in r if x['month']==8]),
               'by_symbol':{s:summary([x for x in r if x['symbol']==s]) for s in SYMBOLS}}
            for n,r in records.items()}
    out=dict(protocol=__doc__,results=result,records=records,promotion_allowed=False)
    Path('impulse_study_results.json').write_text(json.dumps(out,indent=2))
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    run()
