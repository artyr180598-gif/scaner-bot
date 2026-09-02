"""
market_data.py — доставка РЕАЛЬНЫХ рыночных данных для направленного анализа.

Разделение ответственности (важно для честности проверки):

  * `Candles` / `MarketSnapshot` — «сырьё», простые контейнеры без логики.
  * `MarketDataProvider` — интерфейс. Живая реализация ходит в биржу через
    ccxt (`CcxtMarketDataProvider`), офлайн-реализация читает ровно те же
    структуры из JSON (`StaticMarketDataProvider`) — так тесты и проверки на
    исторических/снятых live-данных гоняют ТУ ЖЕ логику, что и прод.
  * Никаких «синтетических» цен здесь нет и быть не должно: если биржа не
    отдала данные, поле остаётся None, и движок сигнала обязан это учесть.

Поддерживаемые источники: любая биржа, доступная в ccxt (по умолчанию —
те, что уже настроены в сканере). Монета ищется по тикеру среди РЕАЛЬНЫХ
рынков биржи, никаких захардкоженных списков.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger("marketdata")

__all__ = [
    "Candles",
    "OrderBookTop",
    "MarketSnapshot",
    "MarketDataProvider",
    "StaticMarketDataProvider",
    "CcxtMarketDataProvider",
    "ScreenRow",
    "parse_okx_candles",
    "correlation",
    "TIMEFRAME_SECONDS",
]

#: Длительность таймфрейма в секундах — нужна для проверки пропусков и свежести.
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
}


@dataclass(frozen=True)
class Candles:
    """
    Серия свечей одного таймфрейма, отсортированная по времени ВОЗРАСТАНИЮ.

    `ts` — время ОТКРЫТИЯ бара в секундах UTC. Последний бар может быть
    незакрытым (`last_closed=False`) — движок это учитывает: решения
    принимаются по закрытым барам, текущая цена берётся отдельно.
    """

    timeframe: str
    ts: tuple[float, ...]
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]
    volume: tuple[float, ...]

    def __len__(self) -> int:  # pragma: no cover - тривиально
        return len(self.close)

    @property
    def step_seconds(self) -> int:
        return TIMEFRAME_SECONDS.get(self.timeframe, 0)

    @property
    def last_ts(self) -> Optional[float]:
        return self.ts[-1] if self.ts else None

    def dropped_last(self) -> "Candles":
        """Копия без последнего (возможно незакрытого) бара."""
        if len(self) <= 1:
            return self
        return Candles(
            timeframe=self.timeframe,
            ts=self.ts[:-1],
            open=self.open[:-1],
            high=self.high[:-1],
            low=self.low[:-1],
            close=self.close[:-1],
            volume=self.volume[:-1],
        )

    @classmethod
    def from_rows(cls, timeframe: str, rows: Sequence[Sequence[float]]) -> "Candles":
        """
        Из формата ccxt fetch_ohlcv: [[ms, o, h, l, c, v], ...].
        Строки сортируются по времени и дедуплицируются по таймстемпу.
        """
        cleaned: dict[float, tuple[float, float, float, float, float]] = {}
        for row in rows:
            if row is None or len(row) < 6:
                continue
            try:
                ts = float(row[0]) / 1000.0
                o, h, l, c, v = (float(row[1]), float(row[2]), float(row[3]),
                                 float(row[4]), float(row[5]))
            except (TypeError, ValueError):
                continue
            if not all(map(_finite_positive, (o, h, l, c))) or v < 0:
                continue
            cleaned[ts] = (o, h, l, c, v)
        keys = sorted(cleaned)
        return cls(
            timeframe=timeframe,
            ts=tuple(keys),
            open=tuple(cleaned[k][0] for k in keys),
            high=tuple(cleaned[k][1] for k in keys),
            low=tuple(cleaned[k][2] for k in keys),
            close=tuple(cleaned[k][3] for k in keys),
            volume=tuple(cleaned[k][4] for k in keys),
        )


def _finite_positive(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf")) and x > 0


@dataclass(frozen=True)
class OrderBookTop:
    """Верх стакана — для оценки ликвидности и стоимости входа."""

    bid: float
    ask: float
    bid_volume_usd: float = 0.0
    ask_volume_usd: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_percent(self) -> float:
        if self.bid <= 0:
            return 0.0
        return (self.ask - self.bid) / self.bid * 100.0


@dataclass
class MarketSnapshot:
    """
    Всё, что удалось РЕАЛЬНО получить по монете на момент анализа.
    Любое поле может быть None — это нормально и учитывается в Data Confidence.
    """

    base: str
    symbol: str
    exchange: str
    fetched_at: float = field(default_factory=time.time)
    last_price: Optional[float] = None
    quote_volume_24h: Optional[float] = None
    price_change_24h_percent: Optional[float] = None
    candles: dict[str, Candles] = field(default_factory=dict)
    book: Optional[OrderBookTop] = None
    funding_rate: Optional[float] = None          # доля за интервал (0.0001 = 0.01%)
    funding_interval_hours: float = 8.0
    next_funding_time: Optional[float] = None
    open_interest_usd: Optional[float] = None
    open_interest_change_percent: Optional[float] = None
    #: отношение объёма лонгов к шортам на перпе (если биржа отдаёт), 1.0 = паритет
    long_short_ratio: Optional[float] = None
    #: корреляция дневных доходностей с BTC за последние N баров (−1..1)
    btc_correlation: Optional[float] = None
    #: изменение цены BTC за последние 24 часа, % — состояние всего рынка.
    #: Ключевой вход для сетапа «разворот после паники»: падение монеты
    #: ВМЕСТЕ с рынком отрабатывает принципиально иначе, чем падение в одиночку.
    market_change_24h_percent: Optional[float] = None
    #: возраст листинга в днях (по самой ранней доступной свече 1d)
    listing_age_days: Optional[float] = None
    errors: list[str] = field(default_factory=list)

    def timeframes(self) -> list[str]:
        return list(self.candles.keys())


@dataclass(frozen=True)
class ScreenRow:
    """
    Строка быстрого скрининга (этап A): дёшево получаемые метрики по ВСЕМ
    парам биржи одним запросом `fetch_tickers`. Тяжёлый разбор свечей (этап B)
    делается только для тех, кто прошёл этот фильтр.
    """

    base: str
    symbol: str
    last: float
    quote_volume_24h: float
    change_24h_percent: Optional[float] = None
    high_24h: Optional[float] = None
    low_24h: Optional[float] = None

    @property
    def range_24h_percent(self) -> Optional[float]:
        """Дневной размах в % от цены — грубая мера волатильности."""
        if not (self.high_24h and self.low_24h and self.last):
            return None
        return (self.high_24h - self.low_24h) / self.last * 100.0

    def screen_score(self) -> float:
        """
        Ранг кандидата: логарифм оборота (ликвидность) × активность движения.
        Это НЕ прогноз, а лишь приоритет «кого разбирать первым».
        """
        vol_term = math.log10(max(self.quote_volume_24h, 1.0))
        move = abs(self.change_24h_percent or 0.0)
        rng = self.range_24h_percent or 0.0
        return vol_term * (1.0 + min(move, 25.0) / 10.0 + min(rng, 40.0) / 20.0)


def correlation(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Коэффициент корреляции Пирсона по доходностям (не по ценам!)."""
    n = min(len(a), len(b))
    if n < 10:
        return None
    ra = [a[i] / a[i - 1] - 1.0 for i in range(len(a) - n + 1, len(a)) if a[i - 1]]
    rb = [b[i] / b[i - 1] - 1.0 for i in range(len(b) - n + 1, len(b)) if b[i - 1]]
    m = min(len(ra), len(rb))
    if m < 10:
        return None
    ra, rb = ra[-m:], rb[-m:]
    ma, mb = sum(ra) / m, sum(rb) / m
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb)


# ---------------------------------------------------------------------------
# Провайдеры
# ---------------------------------------------------------------------------

class MarketDataProvider:
    """Интерфейс источника данных (живой или офлайн)."""

    async def snapshot(
        self, base: str, timeframes: Sequence[str], limit: int = 300
    ) -> MarketSnapshot:  # pragma: no cover - интерфейс
        raise NotImplementedError

    async def universe(self, limit: int = 50) -> list[tuple[str, float]]:
        """Топ монет по 24ч обороту: [(BASE, quote_volume_usd), ...]."""
        raise NotImplementedError  # pragma: no cover

    async def screen(
        self, *, min_quote_volume: float = 0.0, limit: int = 200
    ) -> list[ScreenRow]:
        """Этап A: быстрый скрининг всех пар биржи одним запросом."""
        raise NotImplementedError  # pragma: no cover


class StaticMarketDataProvider(MarketDataProvider):
    """
    Офлайн-провайдер: отдаёт заранее собранные снапшоты.

    Используется в тестах и в проверке на снятых с биржи live-данных —
    гарантирует, что проверяется тот же самый движок, что и в бою.
    """

    def __init__(self, snapshots: dict[str, MarketSnapshot]) -> None:
        self._snapshots = {k.upper(): v for k, v in snapshots.items()}

    async def snapshot(
        self, base: str, timeframes: Sequence[str], limit: int = 300
    ) -> MarketSnapshot:
        snap = self._snapshots.get(base.upper())
        if snap is None:
            return MarketSnapshot(
                base=base.upper(), symbol="", exchange="static",
                errors=[f"нет данных по {base.upper()} в офлайн-наборе"],
            )
        return snap

    async def universe(self, limit: int = 50) -> list[tuple[str, float]]:
        rows = [
            (s.base, s.quote_volume_24h or 0.0) for s in self._snapshots.values()
        ]
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows[:limit]

    async def screen(
        self, *, min_quote_volume: float = 0.0, limit: int = 200
    ) -> list[ScreenRow]:
        rows: list[ScreenRow] = []
        for snap in self._snapshots.values():
            vol = snap.quote_volume_24h or 0.0
            if vol < min_quote_volume:
                continue
            day = snap.candles.get("1d") or snap.candles.get("4h")
            hi = max(day.high[-6:]) if day and len(day) else None
            lo = min(day.low[-6:]) if day and len(day) else None
            rows.append(ScreenRow(
                base=snap.base, symbol=snap.symbol, last=snap.last_price or 0.0,
                quote_volume_24h=vol,
                change_24h_percent=snap.price_change_24h_percent,
                high_24h=hi, low_24h=lo,
            ))
        rows.sort(key=lambda r: r.screen_score(), reverse=True)
        return rows[:limit]


class CcxtMarketDataProvider(MarketDataProvider):
    """
    Живой источник: спот+перп одной биржи через ccxt (async, без ключей).

    Устройство:
      * рынки грузятся один раз и кешируются (`load_markets`) — из них же
        строится «вселенная» монет: НИКАКИХ захардкоженных списков;
      * OHLCV тянется по нужным таймфреймам параллельно;
      * фьючерсные метрики (funding, OI) берутся с перп-рынка, если он есть;
      * любая ошибка сети/биржи не роняет анализ — она попадает в
        `snapshot.errors`, и «уверенность анализа» снижается.
    """

    def __init__(
        self,
        exchange_id: str = "bybit",
        *,
        quote: str = "USDT",
        markets_ttl_seconds: float = 3600.0,
        universe_ttl_seconds: float = 900.0,
        request_timeout_ms: int = 15000,
    ) -> None:
        self.exchange_id = exchange_id
        self.quote = quote.upper()
        self._markets_ttl = markets_ttl_seconds
        self._universe_ttl = universe_ttl_seconds
        self._timeout = request_timeout_ms
        self._spot: Any = None
        self._swap: Any = None
        self._markets_loaded_at = 0.0
        self._universe_cache: list[tuple[str, float]] = []
        self._universe_at = 0.0
        self._ref_cache: dict[tuple[str, str], tuple[float, list[float]]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ init
    def _build(self, market_type: str) -> Any:
        import ccxt.async_support as ccxt_async  # локальный импорт: тесты без ccxt

        klass = getattr(ccxt_async, self.exchange_id)
        return klass({
            "enableRateLimit": True,
            "timeout": self._timeout,
            "options": {"defaultType": market_type},
        })

    async def _ensure_markets(self) -> None:
        async with self._lock:
            fresh = (time.time() - self._markets_loaded_at) < self._markets_ttl
            if self._spot is not None and fresh:
                return
            if self._spot is None:
                self._spot = self._build("spot")
            if self._swap is None:
                try:
                    self._swap = self._build("swap")
                except Exception:  # noqa: BLE001 — биржа без перпов
                    self._swap = None
            await self._spot.load_markets(True)
            if self._swap is not None:
                try:
                    await self._swap.load_markets(True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("perp markets unavailable on %s: %s", self.exchange_id, exc)
                    self._swap = None
            self._markets_loaded_at = time.time()

    async def close(self) -> None:
        for ex in (self._spot, self._swap):
            if ex is not None:
                try:
                    await ex.close()
                except Exception:  # noqa: BLE001
                    pass
        self._spot = None
        self._swap = None

    # ------------------------------------------------------------- вселенная
    async def universe(self, limit: int = 50) -> list[tuple[str, float]]:
        await self._ensure_markets()
        if self._universe_cache and (time.time() - self._universe_at) < self._universe_ttl:
            return self._universe_cache[:limit]
        rows: list[tuple[str, float]] = []
        try:
            tickers = await self._spot.fetch_tickers()
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_tickers failed on %s: %s", self.exchange_id, exc)
            return self._universe_cache[:limit]
        for symbol, ticker in tickers.items():
            if not symbol.endswith(f"/{self.quote}"):
                continue
            base = symbol.split("/")[0]
            vol = ticker.get("quoteVolume")
            if vol is None:
                last, base_vol = ticker.get("last"), ticker.get("baseVolume")
                vol = (last or 0) * (base_vol or 0)
            rows.append((base, float(vol or 0.0)))
        rows.sort(key=lambda r: r[1], reverse=True)
        self._universe_cache = rows
        self._universe_at = time.time()
        return rows[:limit]

    async def screen(
        self, *, min_quote_volume: float = 0.0, limit: int = 200
    ) -> list[ScreenRow]:
        """
        Этап A пайплайна: один `fetch_tickers` по всей бирже → быстрые метрики
        (оборот, изменение за 24ч, дневной размах). Здесь НЕТ технического
        анализа — только отсев мусора и приоритизация кандидатов.
        """
        await self._ensure_markets()
        try:
            tickers = await self._spot.fetch_tickers()
        except Exception as exc:  # noqa: BLE001
            log.warning("screen: fetch_tickers failed on %s: %s", self.exchange_id, exc)
            return []
        rows: list[ScreenRow] = []
        for symbol, t in tickers.items():
            if not symbol.endswith(f"/{self.quote}"):
                continue
            market = self._spot.markets.get(symbol) or {}
            if market.get("active") is False:
                continue
            last = _as_float(t.get("last") or t.get("close"))
            if not last:
                continue
            vol = _as_float(t.get("quoteVolume"))
            if vol is None:
                base_vol = _as_float(t.get("baseVolume")) or 0.0
                vol = base_vol * last
            if (vol or 0.0) < min_quote_volume:
                continue
            rows.append(ScreenRow(
                base=symbol.split("/")[0],
                symbol=symbol,
                last=last,
                quote_volume_24h=float(vol or 0.0),
                change_24h_percent=_as_float(t.get("percentage")),
                high_24h=_as_float(t.get("high")),
                low_24h=_as_float(t.get("low")),
            ))
        rows.sort(key=lambda r: r.screen_score(), reverse=True)
        return rows[:limit]

    async def reference_closes(self, base: str, timeframe: str, limit: int = 200) -> list[float]:
        """Закрытия эталонной монеты (BTC/ETH) — для расчёта корреляции."""
        key = (base.upper(), timeframe)
        cached = self._ref_cache.get(key)
        now = time.time()
        if cached and now - cached[0] < 300.0:
            return cached[1]
        symbol = self.resolve_symbol(base)
        if not symbol:
            return []
        try:
            rows = await self._spot.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception:  # noqa: BLE001
            return []
        closes = [float(r[4]) for r in (rows or []) if r and len(r) > 4]
        self._ref_cache[key] = (now, closes)
        return closes

    def resolve_symbol(self, base: str) -> Optional[str]:
        """Тикер → реальный символ спот-рынка биржи (или None, если нет)."""
        if self._spot is None:
            return None
        candidate = f"{base.upper()}/{self.quote}"
        return candidate if candidate in self._spot.markets else None

    def resolve_swap_symbol(self, base: str) -> Optional[str]:
        if self._swap is None:
            return None
        for candidate in (
            f"{base.upper()}/{self.quote}:{self.quote}",
            f"{base.upper()}/{self.quote}",
        ):
            if candidate in self._swap.markets:
                return candidate
        return None

    # -------------------------------------------------------------- снапшот
    async def snapshot(
        self, base: str, timeframes: Sequence[str], limit: int = 300
    ) -> MarketSnapshot:
        base = base.upper()
        await self._ensure_markets()
        symbol = self.resolve_symbol(base)
        snap = MarketSnapshot(base=base, symbol=symbol or "", exchange=self.exchange_id)
        if symbol is None:
            snap.errors.append(
                f"{base}/{self.quote} нет на бирже {self.exchange_id}"
            )
            return snap

        async def load_tf(tf: str) -> None:
            try:
                rows = await self._spot.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
                candles = Candles.from_rows(tf, rows or [])
                if len(candles):
                    snap.candles[tf] = candles
                else:
                    snap.errors.append(f"пустой OHLCV {tf}")
            except Exception as exc:  # noqa: BLE001
                snap.errors.append(f"OHLCV {tf}: {type(exc).__name__}")

        async def load_ticker() -> None:
            try:
                t = await self._spot.fetch_ticker(symbol)
                snap.last_price = _as_float(t.get("last") or t.get("close"))
                qv = t.get("quoteVolume")
                if qv is None and t.get("baseVolume") and snap.last_price:
                    qv = float(t["baseVolume"]) * snap.last_price
                snap.quote_volume_24h = _as_float(qv)
                snap.price_change_24h_percent = _as_float(t.get("percentage"))
            except Exception as exc:  # noqa: BLE001
                snap.errors.append(f"ticker: {type(exc).__name__}")

        async def load_book() -> None:
            try:
                ob = await self._spot.fetch_order_book(symbol, limit=20)
                bids, asks = ob.get("bids") or [], ob.get("asks") or []
                if bids and asks:
                    bid, ask = float(bids[0][0]), float(asks[0][0])
                    snap.book = OrderBookTop(
                        bid=bid,
                        ask=ask,
                        bid_volume_usd=sum(float(p) * float(a) for p, a in bids[:20]),
                        ask_volume_usd=sum(float(p) * float(a) for p, a in asks[:20]),
                    )
            except Exception as exc:  # noqa: BLE001
                snap.errors.append(f"book: {type(exc).__name__}")

        async def load_derivatives() -> None:
            swap_symbol = self.resolve_swap_symbol(base)
            if not swap_symbol or self._swap is None:
                return
            try:
                fr = await self._swap.fetch_funding_rate(swap_symbol)
                snap.funding_rate = _as_float(fr.get("fundingRate"))
                nxt = fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp")
                if nxt:
                    snap.next_funding_time = float(nxt) / 1000.0
                interval = fr.get("interval") or fr.get("fundingInterval")
                if isinstance(interval, str) and interval.endswith("h"):
                    try:
                        snap.funding_interval_hours = float(interval[:-1])
                    except ValueError:
                        pass
            except Exception as exc:  # noqa: BLE001
                snap.errors.append(f"funding: {type(exc).__name__}")
            try:
                oi = await self._swap.fetch_open_interest(swap_symbol)
                snap.open_interest_usd = _as_float(
                    oi.get("openInterestValue") or oi.get("openInterestAmount")
                )
            except Exception as exc:  # noqa: BLE001
                snap.errors.append(f"OI: {type(exc).__name__}")

        async def load_long_short() -> None:
            swap_symbol = self.resolve_swap_symbol(base)
            if not swap_symbol or self._swap is None:
                return
            fetcher = getattr(self._swap, "fetch_long_short_ratio_history", None)
            if fetcher is None or not self._swap.has.get("fetchLongShortRatioHistory"):
                return
            try:
                rows = await fetcher(swap_symbol, "1h", None, 2)
                if rows:
                    snap.long_short_ratio = _as_float(rows[-1].get("longShortRatio"))
            except Exception:  # noqa: BLE001 — метрика необязательная
                pass

        tasks = [load_tf(tf) for tf in timeframes]
        tasks += [load_ticker(), load_book(), load_derivatives(), load_long_short()]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Корреляция с BTC (по тому же ТФ, что и подтверждающий) — контекст риска
        corr_tf = "1h" if "1h" in snap.candles else (timeframes[-1] if timeframes else None)
        if corr_tf and corr_tf in snap.candles:
            btc_closes = await self.reference_closes("BTC", corr_tf, 200)
            if btc_closes:
                if base != "BTC":
                    snap.btc_correlation = correlation(
                        list(snap.candles[corr_tf].close), btc_closes
                    )
                # состояние всего рынка: изменение BTC за 24 часа
                step_h = max(1.0, TIMEFRAME_SECONDS.get(corr_tf, 3600) / 3600.0)
                back = max(1, int(round(24.0 / step_h)))
                if len(btc_closes) > back and btc_closes[-1 - back] > 0:
                    snap.market_change_24h_percent = (
                        btc_closes[-1] / btc_closes[-1 - back] - 1.0) * 100.0

        # Возраст листинга: если биржа отдала МЕНЬШЕ свечей, чем просили —
        # значит истории физически нет, монета молодая.
        longest = None
        for tf in sorted(snap.candles, key=lambda t: TIMEFRAME_SECONDS.get(t, 0), reverse=True):
            longest = snap.candles[tf]
            break
        if longest is not None and len(longest) < limit:
            step = longest.step_seconds or 0
            if step:
                snap.listing_age_days = len(longest) * step / 86400.0

        snap.fetched_at = time.time()
        return snap


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        return out if out == out else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Разбор ответов бирж «как есть» (используется для офлайн-проверок)
# ---------------------------------------------------------------------------

def parse_okx_candles(timeframe: str, payload: Any) -> Candles:
    """
    OKX /api/v5/market/candles → Candles.

    Формат строки: [ts_ms, o, h, l, c, vol_base, vol_quote, vol_quote2, confirm],
    свечи приходят от НОВЫХ к СТАРЫМ; `confirm == "0"` — бар ещё не закрыт.
    Незакрытый бар сохраняем (он последний), решение о его использовании
    принимает движок.
    """
    data = payload.get("data") if isinstance(payload, dict) else payload
    rows: list[list[float]] = []
    for row in data or []:
        try:
            rows.append([
                float(row[0]), float(row[1]), float(row[2]),
                float(row[3]), float(row[4]), float(row[5]),
            ])
        except (TypeError, ValueError, IndexError):
            continue
    return Candles.from_rows(timeframe, rows)
