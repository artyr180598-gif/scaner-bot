"""CryptoForge Ultimate — real-data Bybit USDT perpetual research engine.

No order execution. No fabricated market values. A signal is an explainable
research idea; its score is NOT a calibrated probability of profit.
"""
from __future__ import annotations

import asyncio
import math
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
import httpx

BYBIT = "https://api.bybit.com"
INTERVALS = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}


@dataclass(slots=True)
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(slots=True)
class Metrics:
    price: float
    ema20: float
    ema50: float
    ema200: float
    rsi: float
    macd: float
    macd_signal: float
    atr: float
    atr_pct: float
    adx: float
    bb_pos: float
    vol_ratio: float
    high20: float
    low20: float
    momentum: float


@dataclass(slots=True)
class Derivatives:
    funding_rate: float = 0.0
    open_interest: float = 0.0
    oi_change_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0
    turnover24h: float = 0.0
    change24h_pct: float = 0.0


@dataclass(slots=True)
class Signal:
    symbol: str
    side: str
    score: float
    timeframe: str
    price: float
    entry_low: float
    entry_high: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    rr1: float
    rr2: float
    rr3: float
    reasons: list[str]
    risks: list[str]
    metrics: dict[str, Any] = field(default_factory=dict)
    generated_at: float = field(default_factory=time.time)


class Bybit:
    def __init__(self, timeout: float = 12.0):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5)),
            headers={"User-Agent": "CryptoForge-Ultimate/2.0"},
        )
        self.sem = asyncio.Semaphore(12)

    async def close(self) -> None:
        await self.client.aclose()

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self.sem:
            last: Exception | None = None
            for attempt in range(3):
                try:
                    r = await self.client.get(BYBIT + path, params=params)
                    r.raise_for_status()
                    data = r.json()
                    if data.get("retCode") != 0:
                        raise RuntimeError(data.get("retMsg", "Bybit error"))
                    return data["result"]
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    last = exc
                    if attempt < 2:
                        await asyncio.sleep(0.35 * (attempt + 1))
            raise RuntimeError(f"Bybit request failed: {last}") from last

    async def instruments(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            p: dict[str, Any] = {"category": "linear", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            x = await self.get("/v5/market/instruments-info", p)
            out.extend(x.get("list", []))
            cursor = x.get("nextPageCursor")
            if not cursor:
                break
        return [
            x for x in out
            if x.get("status") == "Trading"
            and x.get("quoteCoin") == "USDT"
            and x.get("contractType") == "LinearPerpetual"
        ]

    async def tickers(self) -> list[dict[str, Any]]:
        x = await self.get("/v5/market/tickers", {"category": "linear"})
        return x.get("list", [])

    async def candles(self, symbol: str, tf: str, limit: int = 300) -> list[Candle]:
        x = await self.get(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": INTERVALS[tf], "limit": min(limit, 1000)},
        )
        rows = sorted(x.get("list", []), key=lambda z: int(z[0]))
        return [Candle(int(z[0]), float(z[1]), float(z[2]), float(z[3]), float(z[4]), float(z[5])) for z in rows]

    async def oi(self, symbol: str, tf: str = "1h", limit: int = 2) -> list[dict[str, Any]]:
        x = await self.get(
            "/v5/market/open-interest",
            {"category": "linear", "symbol": symbol, "intervalTime": tf, "limit": limit},
        )
        return x.get("list", [])


class TA:
    @staticmethod
    def ema(values: list[float], n: int) -> float:
        if not values:
            return 0.0
        if len(values) < n:
            return sum(values) / len(values)
        k = 2.0 / (n + 1)
        e = sum(values[:n]) / n
        for x in values[n:]:
            e = x * k + e * (1 - k)
        return e

    @staticmethod
    def atr(cs: list[Candle], n: int = 14) -> float:
        if len(cs) < n + 1:
            return 0.0
        tr = [
            max(c.h - c.l, abs(c.h - cs[i - 1].c), abs(c.l - cs[i - 1].c))
            for i, c in enumerate(cs[1:], 1)
        ]
        return sum(tr[-n:]) / n

    @staticmethod
    def rsi(values: list[float], n: int = 14) -> float:
        if len(values) < n + 1:
            return 50.0
        gains: list[float] = []
        losses: list[float] = []
        for i in range(-n, 0):
            d = values[i] - values[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        ag, al = sum(gains) / n, sum(losses) / n
        return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

    @staticmethod
    def adx(cs: list[Candle], n: int = 14) -> float:
        if len(cs) < 2 * n + 2:
            return 0.0
        tr: list[float] = []
        plus: list[float] = []
        minus: list[float] = []
        for i in range(1, len(cs)):
            up = cs[i].h - cs[i - 1].h
            dn = cs[i - 1].l - cs[i].l
            tr.append(max(cs[i].h - cs[i].l, abs(cs[i].h - cs[i - 1].c), abs(cs[i].l - cs[i - 1].c)))
            plus.append(up if up > dn and up > 0 else 0.0)
            minus.append(dn if dn > up and dn > 0 else 0.0)
        dx: list[float] = []
        for j in range(n - 1, len(tr)):
            atr = sum(tr[j - n + 1:j + 1]) or 1.0
            p = 100 * sum(plus[j - n + 1:j + 1]) / atr
            m = 100 * sum(minus[j - n + 1:j + 1]) / atr
            dx.append(100 * abs(p - m) / (p + m) if p + m else 0.0)
        return sum(dx[-n:]) / min(n, len(dx)) if dx else 0.0

    @staticmethod
    def metrics(cs: list[Candle]) -> Metrics:
        closes = [x.c for x in cs]
        p = closes[-1]
        atr = TA.atr(cs)
        ema20, ema50, ema200 = TA.ema(closes, 20), TA.ema(closes, 50), TA.ema(closes, 200)
        macd = TA.ema(closes, 12) - TA.ema(closes, 26)
        # MACD signal is an EMA of recent MACD values, not a single EMA difference.
        macd_series: list[float] = []
        for i in range(max(26, len(closes) - 80), len(closes) + 1):
            sample = closes[:i]
            macd_series.append(TA.ema(sample, 12) - TA.ema(sample, 26))
        macd_signal = TA.ema(macd_series, 9)
        sd = statistics.pstdev(closes[-20:]) if len(closes) >= 20 else 0.0
        mid = ema20
        hi, lo = mid + 2 * sd, mid - 2 * sd
        avg_vol = sum(x.v for x in cs[-21:-1]) / 20 if len(cs) > 21 else 0.0
        return Metrics(
            p, ema20, ema50, ema200, TA.rsi(closes), macd, macd_signal, atr,
            atr / p * 100 if p else 0.0, TA.adx(cs),
            (p - lo) / (hi - lo) if hi > lo else 0.5,
            cs[-1].v / avg_vol if avg_vol else 1.0,
            max(x.h for x in cs[-20:]), min(x.l for x in cs[-20:]),
            (p / closes[-6] - 1) * 100 if len(closes) > 6 else 0.0,
        )


class Scanner:
    def __init__(self, api: Bybit, min_volume: float = 2_000_000.0, max_candidates: int = 120):
        self.api = api
        self.min_volume = min_volume
        self.max_candidates = max_candidates
        self._ticker_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._instrument_cache: tuple[float, dict[str, dict[str, Any]]] | None = None

    async def _tickers(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        if self._ticker_cache and now - self._ticker_cache[0] < 20:
            return self._ticker_cache[1]
        rows = await self.api.tickers()
        out: dict[str, dict[str, Any]] = {}
        for x in rows:
            try:
                if x["symbol"].endswith("USDT") and float(x.get("turnover24h", 0)) >= self.min_volume:
                    out[x["symbol"]] = x
            except (KeyError, TypeError, ValueError):
                continue
        self._ticker_cache = (now, out)
        return out

    async def universe(self) -> dict[str, dict[str, Any]]:
        tickers = await self._tickers()
        now = time.monotonic()
        if not self._instrument_cache or now - self._instrument_cache[0] >= 300:
            instruments = await self.api.instruments()
            self._instrument_cache = (now, {x["symbol"]: x for x in instruments})
        allowed = self._instrument_cache[1]
        return {s: t for s, t in tickers.items() if s in allowed}

    @staticmethod
    def _derivatives(ticker: dict[str, Any], oi_rows: list[dict[str, Any]]) -> Derivatives:
        try:
            bid, ask = float(ticker.get("bid1Price", 0) or 0), float(ticker.get("ask1Price", 0) or 0)
            mid = (bid + ask) / 2 if bid and ask else 0.0
            spread = (ask - bid) / mid * 100 if mid else 0.0
            oi_now = float(ticker.get("openInterest", 0) or 0)
            oi_change = 0.0
            if len(oi_rows) >= 2:
                vals = [float(x.get("openInterest", 0) or 0) for x in oi_rows]
                if vals[-2]:
                    oi_change = (vals[-1] / vals[-2] - 1) * 100
            return Derivatives(
                funding_rate=float(ticker.get("fundingRate", 0) or 0) * 100,
                open_interest=oi_now,
                oi_change_pct=oi_change,
                bid=bid,
                ask=ask,
                spread_pct=spread,
                turnover24h=float(ticker.get("turnover24h", 0) or 0),
                change24h_pct=float(ticker.get("price24hPcnt", 0) or 0) * 100,
            )
        except (TypeError, ValueError):
            return Derivatives()

    async def analyze(self, symbol: str, requested_side: str | None = None, mode: str = "best") -> Signal:
        symbol = symbol.upper().replace("/", "")
        universe = await self.universe()
        if symbol not in universe:
            raise RuntimeError(f"{symbol} не является доступным USDT linear perpetual на Bybit или недостаточно ликвиден")
        tfs = ["15m", "1h", "4h", "1d"] if mode == "best" else (["5m", "15m", "1h"] if mode == "scalp" else ["1h", "4h", "1d"])
        candles = await asyncio.gather(*[self.api.candles(symbol, tf, 300) for tf in tfs])
        ms = {tf: TA.metrics(cs) for tf, cs in zip(tfs, candles) if len(cs) >= 80}
        if "1h" not in ms:
            raise RuntimeError("Недостаточно реальных свечей для подтверждённого анализа")
        oi_rows = await self.api.oi(symbol, "1h", 2)
        d = self._derivatives(universe[symbol], oi_rows)
        weights = {"5m": .08, "15m": .17, "1h": .30, "4h": .30, "1d": .15}
        bull = bear = 0.0
        for tf, m in ms.items():
            w = weights.get(tf, .2)
            trend = (1 if m.ema20 > m.ema50 else -1) + (0.8 if m.ema50 > m.ema200 else -0.8)
            momentum = (1 if m.rsi > 52 else -1 if m.rsi < 48 else 0) + (1 if m.macd > m.macd_signal else -1)
            structure = 1 if m.price > m.ema20 and m.momentum > 0 else -1 if m.price < m.ema20 and m.momentum < 0 else 0
            volume = .6 if m.vol_ratio >= 1.3 else .25 if m.vol_ratio >= 1.05 else 0
            strength = .6 if m.adx >= 25 else .25 if m.adx >= 18 else 0
            s = trend + momentum + structure + volume + strength
            bull += w * max(s, 0)
            bear += w * max(-s, 0)
        side = "LONG" if bull > bear else "SHORT"
        # Derivatives are a confirmation/contrarian filter, never a standalone trigger.
        if d.funding_rate > 0.08:
            bear += .35
        elif d.funding_rate < -0.08:
            bull += .35
        if d.oi_change_pct > 2:
            if d.change24h_pct > 0: bull += .25
            elif d.change24h_pct < 0: bear += .25
        side = "LONG" if bull > bear else "SHORT"
        if requested_side in ("LONG", "SHORT"):
            side = requested_side
        dominant, conflict = max(bull, bear), min(bull, bear)
        score = max(0.0, min(99.0, 50 + dominant * 10 - conflict * 5))
        m = ms["1h"]
        p, atr = m.price, max(m.atr, p * .002)
        if d.spread_pct > 0.25 or m.atr_pct > 15 or m.adx < 12:
            score = min(score, 54.0)
        if side == "LONG":
            entry_low = max(p - .30 * atr, m.low20 + .05 * atr)
            entry_high = p + .08 * atr
            sl = min(entry_low - 1.25 * atr, m.low20 - .12 * atr)
            risk = max(entry_high - sl, atr)
            tp1, tp2, tp3 = entry_high + 1.5 * risk, entry_high + 2.5 * risk, entry_high + 4 * risk
        else:
            entry_low = p - .08 * atr
            entry_high = min(p + .30 * atr, m.high20 - .05 * atr)
            sl = max(entry_high + 1.25 * atr, m.high20 + .12 * atr)
            risk = max(sl - entry_low, atr)
            tp1, tp2, tp3 = entry_low - 1.5 * risk, entry_low - 2.5 * risk, entry_low - 4 * risk
        reasons: list[str] = []
        risks: list[str] = []
        for tf, x in ms.items():
            aligned = (side == "LONG" and x.ema20 > x.ema50) or (side == "SHORT" and x.ema20 < x.ema50)
            if aligned:
                reasons.append(f"{tf}: EMA20/50 поддерживает {side}")
            if (side == "LONG" and x.rsi > 50) or (side == "SHORT" and x.rsi < 50):
                reasons.append(f"{tf}: RSI {x.rsi:.1f} подтверждает импульс")
            if x.vol_ratio >= 1.3:
                reasons.append(f"{tf}: объём {x.vol_ratio:.1f}x среднего")
        if d.funding_rate > .08 and side == "SHORT": reasons.append(f"Funding +{d.funding_rate:.3f}%: перегруженность LONG в пользу SHORT")
        if d.funding_rate < -.08 and side == "LONG": reasons.append(f"Funding {d.funding_rate:.3f}%: перегруженность SHORT в пользу LONG")
        if d.oi_change_pct > 2: reasons.append(f"Open Interest изменился на {d.oi_change_pct:+.2f}% за последний интервал")
        if m.adx < 18: risks.append(f"ADX {m.adx:.1f}: тренд слабый")
        if m.atr_pct > 8: risks.append(f"ATR {m.atr_pct:.1f}%: высокая волатильность")
        if d.spread_pct > .15: risks.append(f"Spread {d.spread_pct:.3f}%: ухудшение ликвидности")
        if m.rsi > 75 or m.rsi < 25: risks.append(f"RSI {m.rsi:.1f}: экстремальная зона")
        if not reasons: reasons.append("Мультифакторное подтверждение слабое")
        if not risks: risks.append("Основной риск — резкая смена рыночного режима")
        return Signal(
            symbol, side, round(score, 1), "1h", p, entry_low, entry_high, sl, tp1, tp2, tp3,
            1.5, 2.5, 4.0, reasons[:10], risks[:6], {
                "rsi": round(m.rsi, 1), "adx": round(m.adx, 1), "atr_pct": round(m.atr_pct, 2),
                "ema20": m.ema20, "ema50": m.ema50, "ema200": m.ema200,
                "macd": m.macd, "macd_signal": m.macd_signal, "volume_ratio": round(m.vol_ratio, 2),
                "bb_pos": round(m.bb_pos, 2), "momentum_pct": round(m.momentum, 2),
                "funding_pct": round(d.funding_rate, 5), "oi_change_pct": round(d.oi_change_pct, 2),
                "open_interest": d.open_interest, "spread_pct": round(d.spread_pct, 4),
                "turnover24h": d.turnover24h, "change24h_pct": round(d.change24h_pct, 2),
            },
        )

    async def scan(self, side: str | None = None, limit: int = 5) -> list[Signal]:
        universe = await self.universe()
        rows = list(universe.values())
        # Stage 1: liquidity + movement. Stage 2: expensive multi-timeframe analysis.
        rows.sort(
            key=lambda x: abs(float(x.get("price24hPcnt", 0))) * math.log10(max(float(x.get("turnover24h", 1)), 1)),
            reverse=True,
        )
        rows = rows[: self.max_candidates]
        sem = asyncio.Semaphore(8)
        async def one(row: dict[str, Any]) -> Signal | None:
            async with sem:
                try:
                    return await self.analyze(row["symbol"], side)
                except Exception:
                    return None
        results = await asyncio.gather(*[one(row) for row in rows])
        signals = [x for x in results if x is not None and x.score >= 60 and (side is None or x.side == side)]
        return sorted(signals, key=lambda x: x.score, reverse=True)[:limit]


class Store:
    def __init__(self, path: str = "data/ultimate.db"):
        self.path = path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS history(
                ts REAL, symbol TEXT, side TEXT, score REAL, entry REAL,
                sl REAL, tp1 REAL, tp2 REAL, tp3 REAL
            )""")
            await db.commit()

    async def save(self, s: Signal) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)",
                (s.generated_at, s.symbol, s.side, s.score, s.entry_low, s.sl, s.tp1, s.tp2, s.tp3),
            )
            await db.commit()

    async def recent(self, n: int = 10) -> list[tuple[Any, ...]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT ts,symbol,side,score,entry,sl,tp1,tp2 FROM history ORDER BY ts DESC LIMIT ?", (n,)
            )
            return await cur.fetchall()


def price(x: float) -> str:
    if x >= 100: return f"{x:,.2f}"
    if x >= 1: return f"{x:.4f}"
    if x >= .01: return f"{x:.6f}"
    return f"{x:.8f}"
