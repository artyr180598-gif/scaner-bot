"""Bounded public archive download, no credentials; no HTTP error retries."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
import urllib.request
import urllib.error
import zipfile
import json

ROOT=Path('rid-depth-data')
SYMBOLS=['DOGEUSDT','WIFUSDT','1000PEPEUSDT']

def get(job):
    symbol,kind,stamp=job
    if kind=='bookDepth':
        suffix=f'daily/bookDepth/{symbol}/{symbol}-bookDepth-{stamp}.zip'
    else:
        suffix=f'monthly/klines/{symbol}/1m/{symbol}-1m-{stamp}.zip'
    path=ROOT/symbol/Path(suffix).name
    url='https://data.binance.vision/data/futures/um/'+suffix
    try:
        if not path.exists():
            with urllib.request.urlopen(url,timeout=45) as response:
                payload=response.read()
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(payload)
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                raise ValueError('CRC failure')
        return {'file':str(path),'status':'ok'}
    except urllib.error.HTTPError as e:
        if e.code in (401,403,429):
            raise RuntimeError(f'Access/rate restriction HTTP {e.code}: stop') from e
        return {'file':str(path),'status':f'http_{e.code}'}
    except (TimeoutError,urllib.error.URLError) as e:
        return {'file':str(path),'status':type(e).__name__}

if __name__=='__main__':
    days=[(date(2026,7,1)+timedelta(days=i)).isoformat() for i in range(62)]
    jobs=[(s,'bookDepth',d) for s in SYMBOLS for d in days]
    jobs += [(s,'klines',m) for s in SYMBOLS for m in ['2026-07','2026-08']]
    results=[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for f in as_completed([pool.submit(get,j) for j in jobs]):
            results.append(f.result())
            if len(results)%24==0:
                print(f'{len(results)}/{len(jobs)} archives',flush=True)
    ROOT.mkdir(exist_ok=True)
    (ROOT/'download_status.json').write_text(json.dumps(results,indent=2))
    print('done',len(results),'errors',[r for r in results if r['status']!='ok'],flush=True)
