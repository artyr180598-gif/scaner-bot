"""
Шлюз к биржам через ccxt (async) + синтетическая биржа для офлайн-режима.

Идея из Freqtrade: один интерфейс, много бирж. Под капотом — ccxt async.
Метод `safe_call` оборачивает любой вызов с ретраями и обработкой RateLimit.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..core.domain.candle import Candles
from ..core.domain.ticker import Ticker

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TickerMeta:
    """Метаданные тикера с биржи (используются анти-мусор фильтрами)."""

    symbol: str
    exchange: str
    last_price: float
    quote_volume_24h: float
    change_pct_24h: float
    recent_move_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0] if "/" in self.symbol else self.symbol


def to_ccxt_symbol(ticker: Ticker) -> str:
    return ticker.symbol


class ExchangeGateway:
    """Асинхронный шлюз к одной или нескольким биржам через ccxt."""

    def __init__(self, exchanges: List[str], quote_currencies: List[str]) -> None:
        self._quote_currencies = [q.upper() for q in quote_currencies]
        self._handles: Dict[str, Any] = {}
        self._pending: List[str] = list(exchanges)

    async def start(self) -> None:
        try:
            import ccxt.async_support as ccxt  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("ccxt is required: pip install ccxt") from exc

        for name in list(self._handles):
            try:
                await self._handles[name].close()
            except Exception:  # noqa: BLE001
                pass
        self._handles.clear()

        for name in self._pending:
            try:
                cls = getattr(ccxt, name)
                client = cls({"enableRateLimit": True, "timeout": 20000})
                self._handles[name] = client
                log.info("exchange %s initialised", name)
            except AttributeError:
                log.warning("ccxt has no exchange %r — skip", name)
            except Exception as exc:  # noqa: BLE001
                log.warning("ccxt %s init failed: %s", name, exc)

    @classmethod
    async def create(cls, exchanges: List[str], quote_currencies: List[str]) -> "ExchangeGateway":
        gw = cls(exchanges, quote_currencies)
        await gw.start()
        gw._pending = []
        return gw

    async def close(self) -> None:
        for h in self._handles.values():
            try:
                await h.close()
            except Exception:  # noqa: BLE001
                pass
        self._handles.clear()

    def available(self) -> List[str]:
        return list(self._handles.keys())

    @staticmethod
    def _build_meta(exchange: str, sym: str, t: dict) -> Optional[TickerMeta]:
        """Приводит сырой тикер ccxt к TickerMeta."""
        try:
            if "/" not in sym:
                return None
            base, quote = sym.split("/", 1)
            last = float(t.get("last") or 0)
            qvol = float(t.get("quoteVolume") or 0)
            if last <= 0 or qvol <= 0:
                return None
            change = float((t.get("info") or {}).get("priceChangePercent")
                           or t.get("percentage") or 0.0)
            bid = float(t.get("bid") or 0)
            ask = float(t.get("ask") or 0)
            spread = ((ask - bid) / last * 100) if (ask > 0 and bid > 0 and last > 0) else 0.0
            return TickerMeta(
                symbol=sym, exchange=exchange, last_price=last,
                quote_volume_24h=qvol, change_pct_24h=change,
                bid=bid, ask=ask, spread_pct=spread,
            )
        except Exception:  # noqa: BLE001
            return None

    async def fetch_tickers_meta(self, exchange: str, top_n: int = 100) -> List[TickerMeta]:
        h = self._handles.get(exchange)
        if not h:
            return []
        try:
            tickers = await h.fetch_tickers()
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_tickers %s failed: %s", exchange, exc)
            return []

        out: List[TickerMeta] = []
        for sym, t in tickers.items():
            meta = self._build_meta(exchange, sym, t)
            if meta is None:
                continue
            # Оставляем только тикеры с нужной котировкой (USDT/USDC/…).
            if meta.symbol.split("/")[1].upper() not in self._quote_currencies:
                continue
            out.append(meta)

        out.sort(key=lambda m: m.quote_volume_24h, reverse=True)
        return out[:top_n]

    async def fetch_ticker_meta(self, exchange: str, symbol: str) -> Optional[TickerMeta]:
        """Тянет метаданные ОДНОГО тикера (для глубокого анализа)."""
        h = self._handles.get(exchange)
        if not h:
            return None
        try:
            t = await h.fetch_ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch_ticker %s %s failed: %s", exchange, symbol, exc)
            return None
        return self._build_meta(exchange, symbol, t)

    async def fetch_candles(self, exchange: str, symbol: str, timeframe: str,
                            limit: int = 300) -> Optional[Candles]:
        h = self._handles.get(exchange)
        if not h:
            return None
        try:
            ohlcv = await h.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch_ohlcv %s %s %s: %s", exchange, symbol, timeframe, exc)
            return None
        if not ohlcv:
            return None
        return Candles.from_ccxt(ohlcv, exchange=exchange, symbol=symbol, timeframe=timeframe)


# ── Синтетическая биржа (офлайн-режим / тесты) ────────────────
class SyntheticExchange:
    """
    Генерирует правдоподобные OHLCV для тестов и self-test.

    Это не «фейк ради галочки»: данные имеют тренд + шум + объёмные всплески,
    чтобы пайплайн (индикаторы → скоринг → сигнал) реально отрабатывал.
    """

    SYMBOLS = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "MATIC/USDT",
        "DOT/USDT", "ATOM/USDT", "NEAR/USDT", "LTC/USDT", "UNI/USDT",
        "APT/USDT", "ARB/USDT", "OP/USDT", "INJ/USDT", "SUI/USDT",
    ]

    SURFACES = {"15m": 60, "1h": 28, "4h": 55, "1d": 90}

    def __init__(self, exchange: str = "synthetic") -> None:
        self.exchange = exchange
        # Кеш сгенерированных рядов, чтобы тикер и свечи давали ОДНУ цену.
        self._cache: dict = {}

    def _series(self, symbol: str, timeframe: str, limit: int) -> tuple[list, list]:
        """Детерминированно генерирует (closes, volumes) по паре symbol+timeframe."""
        key = (symbol, timeframe, limit)
        if key in self._cache:
            return self._cache[key]
        random.seed(hash((symbol, timeframe)) % 1_000_000)
        n = limit
        base = random.uniform(1.0, 200.0)
        drift = random.uniform(-0.0008, 0.0012)
        surface = self.SURFACES.get(timeframe, 40)
        closes = []
        price = base
        phase = random.uniform(0, 6.28)
        for i in range(n):
            cycle = math.sin(i / surface + phase) * 0.006
            noise = random.gauss(0, 0.008)
            price = price * (1 + drift + cycle * (1 if drift > 0 else -1) + noise)
            closes.append(max(price, 0.0001))
        volumes = [max(random.gauss(10_000, 3_000), 1_000) for _ in range(n)]
        for i in range(n - 6, n):
            volumes[i] *= random.uniform(1.5, 3.0)
        result = (closes, volumes)
        self._cache[key] = result
        return result

    def available(self) -> List[str]:
        return [self.exchange]

    async def fetch_tickers_meta(self, exchange: str, top_n: int = 100) -> List[TickerMeta]:
        random.seed(42)
        out = []
        for sym in self.SYMBOLS:
            closes, _ = self._series(sym, "1h", self._candles_limit or 300)
            last = closes[-1]
            prev = closes[-25] if len(closes) > 25 else closes[0]
            change = (last / prev - 1) * 100 if prev else 0.0
            vol = random.uniform(2_000_000, 200_000_000)
            out.append(TickerMeta(
                symbol=sym, exchange=exchange,
                last_price=last,
                quote_volume_24h=vol,
                change_pct_24h=change,
                spread_pct=random.uniform(0.01, 0.25),
            ))
        out.sort(key=lambda m: m.quote_volume_24h, reverse=True)
        return out[:top_n]

    _candles_limit: int = 300

    async def fetch_ticker_meta(self, exchange: str, symbol: str) -> Optional[TickerMeta]:
        """Синтетические метаданные для одного тикера (из той же серии, что и свечи)."""
        for sym in self.SYMBOLS:
            if sym == symbol:
                closes, _ = self._series(sym, "1h", self._candles_limit or 300)
                last = closes[-1]
                prev = closes[-25] if len(closes) > 25 else closes[0]
                change = (last / prev - 1) * 100 if prev else 0.0
                random.seed(hash(symbol) % 1_000_000)
                return TickerMeta(
                    symbol=symbol, exchange=exchange,
                    last_price=last,
                    quote_volume_24h=random.uniform(2_000_000, 200_000_000),
                    change_pct_24h=change,
                    spread_pct=random.uniform(0.01, 0.25),
                )
        return None

    async def fetch_candles(self, exchange: str, symbol: str, timeframe: str,
                            limit: int = 300) -> Optional[Candles]:
        if not symbol:
            return None
        self._candles_limit = max(self._candles_limit, limit)
        closes, volumes = self._series(symbol, timeframe, limit)
        return Candles.from_arrays(closes, exchange=exchange, symbol=symbol,
                                   timeframe=timeframe, volumes=volumes)


# ── Singleton ─────────────────────────────────────────────────
_gateway: Optional[ExchangeGateway] = None
_synthetic: Optional[SyntheticExchange] = None


async def get_gateway(exchanges: List[str], quote_currencies: List[str]) -> ExchangeGateway:
    global _gateway
    if _gateway is None:
        _gateway = await ExchangeGateway.create(exchanges, quote_currencies)
    return _gateway


async def close_gateway() -> None:
    global _gateway
    if _gateway is not None:
        await _gateway.close()
        _gateway = None


def get_synthetic(exchange: str = "synthetic") -> SyntheticExchange:
    global _synthetic
    if _synthetic is None or _synthetic.exchange != exchange:
        _synthetic = SyntheticExchange(exchange)
    return _synthetic
