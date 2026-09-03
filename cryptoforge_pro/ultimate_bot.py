"""CryptoForge Ultimate: real-data Bybit USDT perpetual scanner.

The module intentionally does NOT place orders. It produces explainable trade ideas
from live market data and exposes a small service API used by the Telegram UI.
"""
from __future__ import annotations

import asyncio, math, os, statistics, time
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
import httpx

BYBIT = "https://api.bybit.com"
INTERVALS = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}

@dataclass
class Candle:
    ts: int; o: float; h: float; l: float; c: float; v: float

@dataclass
class Metrics:
    price: float; ema20: float; ema50: float; ema200: float; rsi: float
    macd: float; atr: float; atr_pct: float; adx: float; bb_pos: float
    vol_ratio: float; high20: float; low20: float; momentum: float

@dataclass
class Signal:
    symbol: str; side: str; score: float; timeframe: str; price: float
    entry_low: float; entry_high: float; sl: float; tp1: float; tp2: float; tp3: float
    rr1: float; rr2: float; rr3: float; reasons: list[str]; risks: list[str]
    metrics: dict[str, Any] = field(default_factory=dict); generated_at: float = field(default_factory=time.time)

class Bybit:
    def __init__(self, timeout=12):
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent":"CryptoForge-Ultimate/1.0"})
        self.sem = asyncio.Semaphore(12)
    async def close(self): await self.client.aclose()
    async def get(self, path, params):
        async with self.sem:
            r = await self.client.get(BYBIT + path, params=params)
            r.raise_for_status(); data = r.json()
            if data.get("retCode") != 0: raise RuntimeError(data.get("retMsg", "Bybit error"))
            return data["result"]
    async def instruments(self):
        out=[]; cursor=None
        while True:
            p={"category":"linear","limit":1000}
            if cursor: p["cursor"]=cursor
            x=await self.get("/v5/market/instruments-info",p); out += x.get("list",[]); cursor=x.get("nextPageCursor")
            if not cursor: break
        return [x for x in out if x.get("status")=="Trading" and x.get("quoteCoin")=="USDT" and x.get("contractType")=="LinearPerpetual"]
    async def tickers(self):
        x=await self.get("/v5/market/tickers",{"category":"linear"}); return x.get("list",[])
    async def candles(self,symbol,tf,limit=300):
        x=await self.get("/v5/market/kline",{"category":"linear","symbol":symbol,"interval":INTERVALS[tf],"limit":min(limit,1000)})
        rows=sorted(x.get("list",[]),key=lambda z:int(z[0]))
        return [Candle(int(z[0]),float(z[1]),float(z[2]),float(z[3]),float(z[4]),float(z[5])) for z in rows]
    async def ticker(self,symbol):
        x=await self.get("/v5/market/tickers",{"category":"linear","symbol":symbol}); return x["list"][0]
    async def oi(self,symbol,tf="1h"):
        x=await self.get("/v5/market/open-interest",{"category":"linear","symbol":symbol,"intervalTime":tf,"limit":2}); return x.get("list",[])

class TA:
    @staticmethod
    def ema(a,n):
        if len(a)<n:return a[-1] if a else 0
        k=2/(n+1); e=sum(a[:n])/n
        for x in a[n:]: e=x*k+e*(1-k)
        return e
    @staticmethod
    def atr(cs,n=14):
        if len(cs)<n+1:return 0
        tr=[]
        for i in range(1,len(cs)):
            tr.append(max(cs[i].h-cs[i].l,abs(cs[i].h-cs[i-1].c),abs(cs[i].l-cs[i-1].c)))
        return sum(tr[-n:])/n
    @staticmethod
    def rsi(a,n=14):
        if len(a)<n+1:return 50
        gains=[]; losses=[]
        for i in range(-n,0):
            d=a[i]-a[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
        ag=sum(gains)/n; al=sum(losses)/n
        return 100 if al==0 else 100-(100/(1+ag/al))
    @staticmethod
    def adx(cs,n=14):
        if len(cs)<2*n+2:return 0
        trs=[]; plus=[]; minus=[]
        for i in range(1,len(cs)):
            up=cs[i].h-cs[i-1].h; dn=cs[i-1].l-cs[i].l
            trs.append(max(cs[i].h-cs[i].l,abs(cs[i].h-cs[i-1].c),abs(cs[i].l-cs[i-1].c)))
            plus.append(up if up>dn and up>0 else 0); minus.append(dn if dn>up and dn>0 else 0)
        dx=[]
        for j in range(n,len(trs)):
            tr=sum(trs[j-n+1:j+1]) or 1; p=100*sum(plus[j-n+1:j+1])/tr; m=100*sum(minus[j-n+1:j+1])/tr
            dx.append(100*abs(p-m)/(p+m) if p+m else 0)
        return sum(dx[-n:])/min(n,len(dx)) if dx else 0
    @staticmethod
    def metrics(cs):
        a=[x.c for x in cs]; p=a[-1]; atr=TA.atr(cs); sd=statistics.pstdev(a[-20:]) if len(a)>=20 else 0
        mid=TA.ema(a,20); hi=mid+2*sd; lo=mid-2*sd
        return Metrics(p,TA.ema(a,20),TA.ema(a,50),TA.ema(a,200),TA.rsi(a),TA.ema(a,12)-TA.ema(a,26),atr,(atr/p*100 if p else 0),TA.adx(cs),((p-lo)/(hi-lo) if hi>lo else .5),((cs[-1].v/(sum(x.v for x in cs[-21:-1])/20)) if len(cs)>21 else 1),max(x.h for x in cs[-20:]),min(x.l for x in cs[-20:]),((p/a[-6]-1)*100 if len(a)>6 else 0))

class Scanner:
    def __init__(self, api:Bybit, min_volume=2_000_000): self.api=api; self.min_volume=min_volume
    def _ticker_map(self,rows):
        out={}
        for x in rows:
            try:
                if x["symbol"].endswith("USDT") and float(x.get("turnover24h",0))>=self.min_volume: out[x["symbol"]]=x
            except: pass
        return out
    async def universe(self): return self._ticker_map(await self.api.tickers())
    async def analyze(self,symbol,requested_side=None,mode="best"):
        tfs=["15m","1h","4h","1d"] if mode=="best" else (["5m","15m","1h"] if mode=="scalp" else ["1h","4h","1d"])
        data=await asyncio.gather(*[self.api.candles(symbol,x,300) for x in tfs])
        ms={tf:TA.metrics(cs) for tf,cs in zip(tfs,data) if len(cs)>=80}
        if not ms: raise RuntimeError("Недостаточно реальных свечей")
        weights={"5m":.1,"15m":.2,"1h":.3,"4h":.25,"1d":.15}; bull=bear=0
        for tf,m in ms.items():
            w=weights[tf]
            trend=(1 if m.ema20>m.ema50 else -1)+(0.7 if m.ema50>m.ema200 else -0.7)
            mom=(1 if 52<=m.rsi<=70 else (-1 if 30<=m.rsi<48 else 0))+(1 if m.macd>0 else -1)
            struct=(1 if m.price>m.high20*0.995 else (-1 if m.price<m.low20*1.005 else 0))
            vol=(.6 if m.vol_ratio>=1.3 else 0); strength=(.5 if m.adx>=20 else 0)
            s=trend+mom+struct+vol+strength
            bull += w*max(s,0); bear += w*max(-s,0)
        side="LONG" if bull>bear else "SHORT"
        if requested_side in ("LONG","SHORT"): side=requested_side
        dominant=max(bull,bear); conflict=min(bull,bear)
        score=max(0,min(99,55+dominant*9-conflict*4))
        m=ms.get("1h") or next(iter(ms.values())); p=m.price; atr=max(m.atr,p*.002)
        if side=="LONG":
            el=max(p-.35*atr,m.low20+.05*atr); eh=p+.10*atr; sl=min(el-1.25*atr,m.low20-.15*atr)
            risk=max(eh-sl,atr); tp1=eh+1.5*risk; tp2=eh+2.5*risk; tp3=eh+4*risk
        else:
            el=p-.10*atr; eh=min(p+.35*atr,m.high20-.05*atr); sl=max(eh+1.25*atr,m.high20+.15*atr)
            risk=max(sl-el,atr); tp1=el-1.5*risk; tp2=el-2.5*risk; tp3=el-4*risk
        reasons=[]; risks=[]
        for tf,x in ms.items():
            if (side=="LONG" and x.ema20>x.ema50) or (side=="SHORT" and x.ema20<x.ema50): reasons.append(f"{tf}: тренд подтверждён EMA20/50")
            if (side=="LONG" and x.rsi>50) or (side=="SHORT" and x.rsi<50): reasons.append(f"{tf}: RSI {x.rsi:.1f} поддерживает сторону")
            if x.vol_ratio>=1.3: reasons.append(f"{tf}: объём {x.vol_ratio:.1f}x от среднего")
        if m.adx<18: risks.append(f"ADX {m.adx:.1f}: тренд слабый")
        if m.atr_pct>8: risks.append(f"ATR {m.atr_pct:.1f}%: высокая волатильность")
        if m.rsi>75 or m.rsi<25: risks.append(f"RSI {m.rsi:.1f}: рынок перегрет/перепродан")
        if not risks: risks.append("Основной риск — резкое изменение рыночного режима")
        return Signal(symbol,side,round(score,1),"1h",p,el,eh,sl,tp1,tp2,tp3,1.5,2.5,4.0,reasons[:8],risks[:5],{"rsi":round(m.rsi,1),"adx":round(m.adx,1),"atr_pct":round(m.atr_pct,2),"ema20":m.ema20,"ema50":m.ema50,"ema200":m.ema200,"volume_ratio":round(m.vol_ratio,2),"bb_pos":round(m.bb_pos,2)})
    async def scan(self,side=None,limit=5):
        universe=await self.universe(); rows=list(universe.values())
        rows.sort(key=lambda x:abs(float(x.get("price24hPcnt",0))) * math.log10(max(float(x.get("turnover24h",1)),1)),reverse=True)
        sem=asyncio.Semaphore(8)
        async def one(x):
            async with sem:
                try:return await self.analyze(x["symbol"],side)
                except Exception:return None
        sigs=[x for x in await asyncio.gather(*[one(x) for x in rows[:120]]) if x]
        sigs=[x for x in sigs if x.score>=60 and (side is None or x.side==side)]
        return sorted(sigs,key=lambda x:x.score,reverse=True)[:limit]

class Store:
    def __init__(self,path="data/ultimate.db"): self.path=path
    async def init(self):
        os.makedirs(os.path.dirname(self.path) or ".",exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS history(ts REAL,symbol TEXT,side TEXT,score REAL,entry REAL,sl REAL,tp1 REAL,tp2 REAL,tp3 REAL)"); await db.commit()
    async def save(self,s):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)",(s.generated_at,s.symbol,s.side,s.score,s.entry_low,s.sl,s.tp1,s.tp2,s.tp3)); await db.commit()
    async def recent(self,n=10):
        async with aiosqlite.connect(self.path) as db:
            cur=await db.execute("SELECT ts,symbol,side,score,entry,sl,tp1,tp2 FROM history ORDER BY ts DESC LIMIT ?",(n,)); return await cur.fetchall()


def price(x):
    if x>=100:return f"{x:,.2f}"
    if x>=1:return f"{x:.4f}"
    if x>=.01:return f"{x:.6f}"
    return f"{x:.8f}"
