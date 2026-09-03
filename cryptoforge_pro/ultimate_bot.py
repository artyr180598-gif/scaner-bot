"""CryptoForge Ultimate — real-data Bybit USDT perpetual research engine.

No order execution. No fabricated market values. A signal is an explainable
research idea; its score is NOT a calibrated probability of profit.
"""
from __future__ import annotations

import asyncio, math, os, statistics, time
from dataclasses import dataclass, field
from typing import Any
import aiosqlite, httpx

BYBIT = "https://api.bybit.com"
INTERVALS = {"5m":"5","15m":"15","1h":"60","4h":"240","1d":"D"}

@dataclass(slots=True)
class Candle:
    ts:int; o:float; h:float; l:float; c:float; v:float
@dataclass(slots=True)
class Metrics:
    price:float; ema20:float; ema50:float; ema200:float; rsi:float; macd:float; macd_signal:float
    atr:float; atr_pct:float; adx:float; bb_pos:float; vol_ratio:float; high20:float; low20:float; momentum:float
@dataclass(slots=True)
class Derivatives:
    funding_rate:float=0.0; open_interest:float=0.0; oi_change_pct:float=0.0; bid:float=0.0; ask:float=0.0
    spread_pct:float=0.0; turnover24h:float=0.0; change24h_pct:float=0.0
@dataclass(slots=True)
class Signal:
    symbol:str; side:str; score:float; timeframe:str; price:float; entry_low:float; entry_high:float; sl:float
    tp1:float; tp2:float; tp3:float; rr1:float; rr2:float; rr3:float; reasons:list[str]; risks:list[str]
    metrics:dict[str,Any]=field(default_factory=dict); generated_at:float=field(default_factory=time.time)

class Bybit:
    def __init__(self, timeout:float=12.0):
        self.client=httpx.AsyncClient(timeout=httpx.Timeout(timeout,connect=min(timeout,5)),headers={"User-Agent":"CryptoForge-Ultimate/2.0"})
        self.sem=asyncio.Semaphore(12)
    async def close(self): await self.client.aclose()
    async def get(self,path:str,params:dict[str,Any])->dict[str,Any]:
        async with self.sem:
            last=None
            for attempt in range(3):
                try:
                    r=await self.client.get(BYBIT+path,params=params); r.raise_for_status(); data=r.json()
                    if data.get("retCode")!=0: raise RuntimeError(data.get("retMsg","Bybit error"))
                    return data["result"]
                except (httpx.HTTPError,RuntimeError,ValueError) as exc:
                    last=exc
                    if attempt<2: await asyncio.sleep(.35*(attempt+1))
            raise RuntimeError(f"Bybit request failed: {last}") from last
    async def instruments(self):
        out=[]; cursor=None
        while True:
            p={"category":"linear","limit":1000};
            if cursor:p["cursor"]=cursor
            x=await self.get("/v5/market/instruments-info",p); out+=x.get("list",[]); cursor=x.get("nextPageCursor")
            if not cursor:break
        return [x for x in out if x.get("status")=="Trading" and x.get("quoteCoin")=="USDT" and x.get("contractType")=="LinearPerpetual"]
    async def tickers(self): return (await self.get("/v5/market/tickers",{"category":"linear"})).get("list",[])
    async def candles(self,symbol,tf,limit=300):
        x=await self.get("/v5/market/kline",{"category":"linear","symbol":symbol,"interval":INTERVALS[tf],"limit":min(limit,1000)})
        rows=sorted(x.get("list",[]),key=lambda z:int(z[0]))
        return [Candle(int(z[0]),float(z[1]),float(z[2]),float(z[3]),float(z[4]),float(z[5])) for z in rows]
    async def oi(self,symbol,tf="1h",limit=2): return (await self.get("/v5/market/open-interest",{"category":"linear","symbol":symbol,"intervalTime":tf,"limit":limit})).get("list",[])

class TA:
    @staticmethod
    def ema(v,n):
        if not v:return 0.0
        if len(v)<n:return sum(v)/len(v)
        k=2/(n+1); e=sum(v[:n])/n
        for x in v[n:]:e=x*k+e*(1-k)
        return e
    @staticmethod
    def atr(cs,n=14):
        if len(cs)<n+1:return 0.0
        tr=[max(c.h-c.l,abs(c.h-cs[i-1].c),abs(c.l-cs[i-1].c)) for i,c in enumerate(cs[1:],1)]
        return sum(tr[-n:])/n
    @staticmethod
    def rsi(v,n=14):
        if len(v)<n+1:return 50.0
        g=[];l=[]
        for i in range(-n,0):
            d=v[i]-v[i-1];g.append(max(d,0));l.append(max(-d,0))
        ag,al=sum(g)/n,sum(l)/n
        return 100.0 if al==0 else 100-100/(1+ag/al)
    @staticmethod
    def adx(cs,n=14):
        if len(cs)<2*n+2:return 0.0
        tr=[];plus=[];minus=[]
        for i in range(1,len(cs)):
            up=cs[i].h-cs[i-1].h;dn=cs[i-1].l-cs[i].l
            tr.append(max(cs[i].h-cs[i].l,abs(cs[i].h-cs[i-1].c),abs(cs[i].l-cs[i-1].c)))
            plus.append(up if up>dn and up>0 else 0);minus.append(dn if dn>up and dn>0 else 0)
        dx=[]
        for j in range(n-1,len(tr)):
            a=sum(tr[j-n+1:j+1]) or 1;p=100*sum(plus[j-n+1:j+1])/a;m=100*sum(minus[j-n+1:j+1])/a
            dx.append(100*abs(p-m)/(p+m) if p+m else 0)
        return sum(dx[-n:])/min(n,len(dx)) if dx else 0
    @staticmethod
    def metrics(cs):
        v=[x.c for x in cs];p=v[-1];atr=TA.atr(cs);e20,e50,e200=TA.ema(v,20),TA.ema(v,50),TA.ema(v,200)
        macd_series=[TA.ema(v[:i],12)-TA.ema(v[:i],26) for i in range(max(26,len(v)-80),len(v)+1)]
        macd=macd_series[-1];ms=TA.ema(macd_series,9);sd=statistics.pstdev(v[-20:]) if len(v)>=20 else 0;hi=e20+2*sd;lo=e20-2*sd
        av=sum(x.v for x in cs[-21:-1])/20 if len(cs)>21 else 0
        return Metrics(p,e20,e50,e200,TA.rsi(v),macd,ms,atr,atr/p*100 if p else 0,TA.adx(cs),(p-lo)/(hi-lo) if hi>lo else .5,cs[-1].v/av if av else 1,max(x.h for x in cs[-20:]),min(x.l for x in cs[-20:]),(p/v[-6]-1)*100 if len(v)>6 else 0)

class Scanner:
    def __init__(self,api:Bybit,min_volume=2_000_000,max_candidates=120):
        self.api=api;self.min_volume=min_volume;self.max_candidates=max_candidates;self._ticker_cache=None;self._instrument_cache=None
    async def _tickers(self):
        now=time.monotonic()
        if self._ticker_cache and now-self._ticker_cache[0]<20:return self._ticker_cache[1]
        out={}
        for x in await self.api.tickers():
            try:
                if x["symbol"].endswith("USDT") and float(x.get("turnover24h",0))>=self.min_volume:out[x["symbol"]]=x
            except (KeyError,TypeError,ValueError):pass
        self._ticker_cache=(now,out);return out
    async def universe(self):
        tickers=await self._tickers();now=time.monotonic()
        if not self._instrument_cache or now-self._instrument_cache[0]>=300:
            ins=await self.api.instruments();self._instrument_cache=(now,{x["symbol"]:x for x in ins})
        return {s:t for s,t in tickers.items() if s in self._instrument_cache[1]}
    @staticmethod
    def _derivatives(t,rows):
        try:
            bid,ask=float(t.get("bid1Price",0) or 0),float(t.get("ask1Price",0) or 0);mid=(bid+ask)/2 if bid and ask else 0
            oi=float(t.get("openInterest",0) or 0);chg=0
            if len(rows)>=2:
                vals=[float(x.get("openInterest",0) or 0) for x in rows]
                if vals[-2]:chg=(vals[-1]/vals[-2]-1)*100
            return Derivatives(float(t.get("fundingRate",0) or 0)*100,oi,chg,bid,ask,(ask-bid)/mid*100 if mid else 0,float(t.get("turnover24h",0) or 0),float(t.get("price24hPcnt",0) or 0)*100)
        except (TypeError,ValueError):return Derivatives()
    async def analyze(self,symbol,requested_side=None,mode="best"):
        symbol=symbol.upper().replace("/","");u=await self.universe()
        if symbol not in u:raise RuntimeError(f"{symbol} не является доступным ликвидным USDT linear perpetual на Bybit")
        tfs=["15m","1h","4h","1d"] if mode=="best" else (["5m","15m","1h"] if mode=="scalp" else ["1h","4h","1d"])
        candles=await asyncio.gather(*[self.api.candles(symbol,tf,300) for tf in tfs]);ms={tf:TA.metrics(cs) for tf,cs in zip(tfs,candles) if len(cs)>=80}
        if "1h" not in ms:raise RuntimeError("Недостаточно реальных свечей")
        d=self._derivatives(u[symbol],await self.api.oi(symbol,"1h",2));weights={"5m":.08,"15m":.17,"1h":.30,"4h":.30,"1d":.15};bull=bear=0
        for tf,m in ms.items():
            w=weights.get(tf,.2);trend=(1 if m.ema20>m.ema50 else -1)+(0.8 if m.ema50>m.ema200 else -0.8);mom=(1 if m.rsi>52 else -1 if m.rsi<48 else 0)+(1 if m.macd>m.macd_signal else -1);struct=1 if m.price>m.ema20 and m.momentum>0 else -1 if m.price<m.ema20 and m.momentum<0 else 0;vol=.6 if m.vol_ratio>=1.3 else .25 if m.vol_ratio>=1.05 else 0;strength=.6 if m.adx>=25 else .25 if m.adx>=18 else 0;s=trend+mom+struct+vol+strength;bull+=w*max(s,0);bear+=w*max(-s,0)
        if d.funding_rate>.08:bear+=.35
        elif d.funding_rate<-.08:bull+=.35
        if d.oi_change_pct>2:
            if d.change24h_pct>0:bull+=.25
            elif d.change24h_pct<0:bear+=.25
        side="LONG" if bull>bear else "SHORT";dominant,conflict=max(bull,bear),min(bull,bear);score=max(0,min(99,50+dominant*10-conflict*5))
        m=ms["1h"];p=m.price;atr=max(m.atr,p*.002)
        if d.spread_pct>.25 or m.atr_pct>15 or m.adx<12:score=min(score,54)
        if requested_side in ("LONG","SHORT") and score>=60:side=requested_side
        if score<60:side="NO TRADE"
        if side=="LONG":
            el=max(p-.30*atr,m.low20+.05*atr);eh=p+.08*atr;sl=min(el-1.25*atr,m.low20-.12*atr);risk=max(eh-sl,atr);tp1,tp2,tp3=eh+1.5*risk,eh+2.5*risk,eh+4*risk
        elif side=="SHORT":
            el=p-.08*atr;eh=min(p+.30*atr,m.high20-.05*atr);sl=max(eh+1.25*atr,m.high20+.12*atr);risk=max(sl-el,atr);tp1,tp2,tp3=el-1.5*risk,el-2.5*risk,el-4*risk
        else:el=eh=p;sl=tp1=tp2=tp3=p;risk=atr
        reasons=[];risks=[]
        for tf,x in ms.items():
            if (side=="LONG" and x.ema20>x.ema50) or (side=="SHORT" and x.ema20<x.ema50):reasons.append(f"{tf}: EMA20/50 поддерживает {side}")
            if (side=="LONG" and x.rsi>50) or (side=="SHORT" and x.rsi<50):reasons.append(f"{tf}: RSI {x.rsi:.1f} подтверждает импульс")
            if x.vol_ratio>=1.3:reasons.append(f"{tf}: объём {x.vol_ratio:.1f}x среднего")
        if d.funding_rate>.08 and side=="SHORT":reasons.append(f"Funding +{d.funding_rate:.3f}%: перегруженность LONG")
        if d.funding_rate<-.08 and side=="LONG":reasons.append(f"Funding {d.funding_rate:.3f}%: перегруженность SHORT")
        if d.oi_change_pct>2:reasons.append(f"Open Interest {d.oi_change_pct:+.2f}% за последний интервал")
        if m.adx<18:risks.append(f"ADX {m.adx:.1f}: тренд слабый")
        if m.atr_pct>8:risks.append(f"ATR {m.atr_pct:.1f}%: высокая волатильность")
        if d.spread_pct>.15:risks.append(f"Spread {d.spread_pct:.3f}%: ликвидность хуже обычной")
        if m.rsi>75 or m.rsi<25:risks.append(f"RSI {m.rsi:.1f}: экстремальная зона")
        if score<60:risks.append("Сетап не прошёл минимальный фильтр качества — сделка не рекомендуется")
        if not reasons:reasons.append("Недостаточно подтверждений")
        if not risks:risks.append("Основной риск — резкая смена рыночного режима")
        return Signal(symbol,side,round(score,1),"1h",p,el,eh,sl,tp1,tp2,tp3,1.5,2.5,4.0,reasons[:10],risks[:6],{"rsi":round(m.rsi,1),"adx":round(m.adx,1),"atr_pct":round(m.atr_pct,2),"ema20":m.ema20,"ema50":m.ema50,"ema200":m.ema200,"macd":m.macd,"macd_signal":m.macd_signal,"volume_ratio":round(m.vol_ratio,2),"bb_pos":round(m.bb_pos,2),"momentum_pct":round(m.momentum,2),"funding_pct":round(d.funding_rate,5),"oi_change_pct":round(d.oi_change_pct,2),"open_interest":d.open_interest,"spread_pct":round(d.spread_pct,4),"turnover24h":d.turnover24h,"change24h_pct":round(d.change24h_pct,2)})
    async def scan(self,side=None,limit=5):
        rows=list((await self.universe()).values());rows.sort(key=lambda x:abs(float(x.get("price24hPcnt",0)))*math.log10(max(float(x.get("turnover24h",1)),1)),reverse=True);rows=rows[:self.max_candidates];sem=asyncio.Semaphore(8)
        async def one(row):
            async with sem:
                try:return await self.analyze(row["symbol"],side)
                except Exception:return None
        results=await asyncio.gather(*[one(r) for r in rows]);signals=[x for x in results if x and x.side!="NO TRADE" and x.score>=60 and (side is None or x.side==side)]
        return sorted(signals,key=lambda x:x.score,reverse=True)[:limit]

class Store:
    def __init__(self,path="data/ultimate.db"):self.path=path
    async def init(self):
        os.makedirs(os.path.dirname(self.path) or ".",exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS history(ts REAL,symbol TEXT,side TEXT,score REAL,entry REAL,sl REAL,tp1 REAL,tp2 REAL,tp3 REAL)");await db.commit()
    async def save(self,s):
        async with aiosqlite.connect(self.path) as db:await db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)",(s.generated_at,s.symbol,s.side,s.score,s.entry_low,s.sl,s.tp1,s.tp2,s.tp3));await db.commit()
    async def recent(self,n=10):
        async with aiosqlite.connect(self.path) as db:
            cur=await db.execute("SELECT ts,symbol,side,score,entry,sl,tp1,tp2 FROM history ORDER BY ts DESC LIMIT ?",(n,));return await cur.fetchall()

def price(x):
    if x>=100:return f"{x:,.2f}"
    if x>=1:return f"{x:.4f}"
    if x>=.01:return f"{x:.6f}"
    return f"{x:.8f}"
