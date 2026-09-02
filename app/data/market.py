"""
app/data/market.py — сервис рыночных данных.

Отвечает за:
  * вселенную монет (фильтр по обороту и «мусорным» тикерам);
  * свечи по всем таймфреймам (старшие ТФ собираются ресемплингом — экономим
    запросы, как это делает freqtrade с informative pairs);
  * тикеры, funding, open interest и их динамику;
  * общий фон рынка (BTC + ширина рынка);
  * переключение между биржами: если одна легла, данные берём с другой.

Сервис НЕ считает индикаторы — только добывает и нормализует данные.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from app.config.settings import Settings
from app.data.cache import TTLCache
from app.data.exchanges import ExchangeGateway, create_gateways
from app.domain.models import (Candles, DerivativesInfo, MarketContext,
                               MarketSnapshot, TickerInfo, Timeframe)
from app.indicators import core as ind
from app.utils.errors import DataError, ExchangeUnavailable, gather_safe

log = logging.getLogger(__name__)

LEVERAGED_SUFFIX = re.compile(r"(3L|3S|5L|5S|2L|2S|UP|DOWN|BULL|BEAR)$", re.I)


class MarketDataService:
    """Единая точка доступа к рыночным данным (с кэшем и failover)."""

    def __init__(
        self,
        settings: Settings,
        gateways: Optional[Sequence[ExchangeGateway]] = None,
        derivatives_gateways: Optional[Sequence[ExchangeGateway]] = None,
    ) -> None:
        self.settings = settings
        self.gateways: List[ExchangeGateway] = list(gateways or [])
        self.derivatives_gateways: List[ExchangeGateway] = list(
            derivatives_gateways or [])
        self.candles_cache = TTLCache(settings.cache_ttl_seconds, maxsize=8192)
        self.ticker_cache = TTLCache(max(settings.cache_ttl_seconds, 30), maxsize=8)
        self._own_gateways = not gateways

    # ------------------------------------------------------------------
    @classmethod
    async def create(cls, settings: Settings) -> "MarketDataService":
        """Создаёт сервис и подключается к биржам (используется на старте бота)."""
        gateways = await create_gateways(settings.exchanges, settings)
        deriv_names = [n for n in settings.derivatives_exchanges
                       if n not in settings.exchanges]
        deriv = await create_gateways(deriv_names, settings) if deriv_names else []
        return cls(settings, gateways, deriv)

    async def close(self) -> None:
        for gw in self.gateways + self.derivatives_gateways:
            await gw.close()

    def health(self) -> List[str]:
        return [gw.describe() for gw in self.gateways + self.derivatives_gateways]

    # ------------------------------------------------------------------
    # Тикеры и вселенная
    # ------------------------------------------------------------------
    async def tickers(self, refresh: bool = False) -> Dict[str, TickerInfo]:
        """Тикеры основной биржи (кэш на TTL). Fallback на следующую биржу."""
        async def _produce() -> Dict[str, TickerInfo]:
            last_error: Optional[Exception] = None
            for gw in self.gateways:
                try:
                    raw = await gw.fetch_tickers()
                    return self._normalize_tickers(raw)
                except DataError as exc:
                    last_error = exc
                    log.warning("%s: тикеры недоступны (%s), пробуем дальше", gw.name, exc)
            raise DataError(f"тикеры недоступны ни на одной бирже: {last_error}")

        if refresh:
            self.ticker_cache.clear()
        try:
            return await self.ticker_cache.get_or_set("tickers", _produce)
        except DataError:
            return {}

    def _normalize_tickers(self, raw: Dict[str, Dict]) -> Dict[str, TickerInfo]:
        quote = self.settings.quote
        out: Dict[str, TickerInfo] = {}
        for symbol, t in (raw or {}).items():
            if not symbol or "/" not in symbol:
                continue
            base, q = symbol.split("/")[:2]
            if q != quote:
                continue
            if self._is_excluded(base):
                continue
            last = _f(t.get("last")) or _f(t.get("close"))
            qv = _f(t.get("quoteVolume")) or 0.0
            bv = _f(t.get("baseVolume")) or 0.0
            if not qv and bv and last:
                qv = bv * last
            out[symbol] = TickerInfo(
                symbol=symbol,
                last=last or float("nan"),
                quote_volume=qv,
                base_volume=bv,
                change_pct=_f(t.get("percentage")) or 0.0,
                high=_f(t.get("high")) or float("nan"),
                low=_f(t.get("low")) or float("nan"),
                bid=_f(t.get("bid")) or float("nan"),
                ask=_f(t.get("ask")) or float("nan"),
            )
        return out

    def _is_excluded(self, base: str) -> bool:
        base = base.upper()
        if base in {s.upper() for s in self.settings.exclude_symbols}:
            return True
        if self.settings.exclude_leveraged_tokens and LEVERAGED_SUFFIX.search(base):
            return True
        return False

    async def universe(self, limit: Optional[int] = None) -> List[str]:
        """
        Список пар для анализа: топ по обороту + явно включённые.

        Именно здесь реализуется «не берём мусор»: оборот ниже порога и
        стейблкоины/леверидж-токены отбрасываются до всякого анализа.
        """
        tickers = await self.tickers()
        if not tickers:
            return []
        min_vol = self.settings.min_quote_volume_usd
        ranked = sorted(
            (s for s, t in tickers.items() if t.quote_volume >= min_vol),
            key=lambda s: -tickers[s].quote_volume,
        )
        limit = limit or self.settings.max_universe
        universe = ranked[:limit]
        for base in self.settings.include_symbols:
            symbol = f"{base}/{self.settings.quote}"
            if symbol in tickers and symbol not in universe:
                universe.append(symbol)
        return universe

    def universe_stats(self, tickers: Dict[str, TickerInfo]) -> Dict[str, float]:
        """Медиана/разброс изменений за 24 ч — для кросс-секционного контекста."""
        changes = [t.change_pct for t in tickers.values() if t.change_pct == t.change_pct]
        if not changes:
            return {}
        import statistics

        median = statistics.median(changes)
        std = statistics.pstdev(changes) or 1.0
        return {
            "change_24h_median": float(median),
            "change_24h_std": float(std),
            "positive_ratio": float(sum(1 for c in changes if c > 0) / len(changes)),
            "count": float(len(changes)),
        }

    # ------------------------------------------------------------------
    # Свечи
    # ------------------------------------------------------------------
    async def candles(self, symbol: str, tf: Timeframe, limit: int) -> Optional[Candles]:
        """Свечи одного ТФ (кэш + перебор бирж)."""
        key = (symbol, tf.value, limit)

        async def _produce() -> Optional[Candles]:
            last_error: Optional[Exception] = None
            for gw in self.gateways:
                try:
                    rows = await gw.fetch_ohlcv(symbol, tf.value, limit)
                    if not rows:
                        last_error = DataError(f"{gw.name}: пустой ответ")
                        continue
                    candles = Candles.from_raw(symbol, tf, rows, source=gw.name,
                                               last_closed=False)
                    return self._mark_closed(candles, tf)
                except DataError as exc:
                    last_error = exc
                    continue
            if last_error:
                log.debug("%s %s: свечи недоступны (%s)", symbol, tf.value, last_error)
            return None

        return await self.candles_cache.get_or_set(key, _produce)

    @staticmethod
    def _mark_closed(candles: Candles, tf: Timeframe) -> Candles:
        """
        Помечает, закрыт ли последний бар.

        Биржа отдаёт текущий (ещё формирующийся) бар; решения по нему принимать
        нельзя — иначе live-сигнал разойдётся с бектестом.
        """
        if candles.empty:
            return candles
        bar_end = candles.last_timestamp.timestamp() + tf.minutes * 60
        candles.last_closed = bar_end <= datetime.now(timezone.utc).timestamp()
        return candles

    async def snapshot(self, symbol: str,
                       tickers: Optional[Dict[str, TickerInfo]] = None,
                       stats: Optional[Dict[str, float]] = None) -> Optional[MarketSnapshot]:
        """Полный снимок по монете: свечи всех ТФ + тикер + деривативы."""
        base, quote = symbol.split("/")[:2]
        settings = self.settings
        warnings: List[str] = []

        base_candles = await self.candles(symbol, settings.base_timeframe, settings.bars_base)
        if base_candles is None or base_candles.empty:
            log.debug("%s: нет базовых свечей", symbol)
            return None

        candles_map: Dict[Timeframe, Candles] = {settings.base_timeframe: base_candles}

        # Старшие ТФ: дневки качаем отдельно (глубокая история), остальные —
        # ресемплинг из базового ТФ (экономия запросов к бирже).
        daily_tf = Timeframe.D1
        if daily_tf in settings.analysis_timeframes:
            daily = await self.candles(symbol, daily_tf, settings.bars_daily)
            if daily is not None and not daily.empty:
                candles_map[daily_tf] = daily
            else:
                warnings.append("дневные свечи недоступны")

        for tf in settings.analysis_timeframes:
            if tf in candles_map or tf.minutes <= settings.base_timeframe.minutes:
                continue
            if tf is daily_tf:
                continue
            try:
                candles_map[tf] = base_candles.resample(tf)
            except ValueError as exc:
                warnings.append(f"{tf.value}: {exc}")

        ticker = (tickers or {}).get(symbol)
        derivatives = await self.derivatives(base, quote)

        snapshot = MarketSnapshot(
            symbol=symbol, base=base, quote=quote,
            exchange=(base_candles.source or (self.gateways[0].name if self.gateways else "")),
            candles=candles_map, ticker=ticker, derivatives=derivatives,
            universe_stats=dict(stats or {}), warnings=warnings,
        )
        return snapshot

    async def snapshots(self, symbols: Sequence[str],
                        concurrency: Optional[int] = None) -> List[MarketSnapshot]:
        """Параллельная сборка снимков (ошибочные пропускаются)."""
        tickers = await self.tickers()
        stats = self.universe_stats(tickers)
        sem = asyncio.Semaphore(concurrency or self.settings.deep_analysis_concurrency)

        async def _one(symbol: str) -> Optional[MarketSnapshot]:
            async with sem:
                try:
                    return await self.snapshot(symbol, tickers, stats)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: снимок не собран: %s", symbol, exc)
                    return None

        results = await gather_safe([_one(s) for s in symbols], label="snapshot")
        return [r for r in results if r is not None]

    # ------------------------------------------------------------------
    # Деривативы
    # ------------------------------------------------------------------
    async def derivatives(self, base: str, quote: str) -> DerivativesInfo:
        """Funding + OI + динамика OI. Полностью опционально: нет — значит нет."""
        info = DerivativesInfo()
        gateways = self.derivatives_gateways or self.gateways
        if not gateways:
            return info
        gw = gateways[0]
        swap = gw.swap_symbol(base, quote)
        funding, oi, oi_hist = await asyncio.gather(
            gw.fetch_funding_rate(swap),
            gw.fetch_open_interest(swap),
            gw.fetch_open_interest_history(swap, "1h", 26),
            return_exceptions=True,
        )
        if isinstance(funding, dict) and funding:
            rate = _f(funding.get("fundingRate"))
            if rate is not None:
                info.funding_rate = float(rate)
                ts = funding.get("fundingTimestamp") or funding.get("nextFundingTimestamp")
                info.mark_price = _f(funding.get("markPrice"))
                info.index_price = _f(funding.get("indexPrice"))
                info.available = True
                if ts:
                    info.funding_interval_hours = 8
        if isinstance(oi, dict) and oi:
            info.open_interest = _f(oi.get("openInterestAmount")) or _f(oi.get("openInterestBase"))
            info.open_interest_value = _f(oi.get("openInterestValue"))
            info.available = True
        if isinstance(oi_hist, list) and len(oi_hist) >= 2:
            values = [(_f(row.get("openInterestAmount")) or _f(row.get("openInterestValue")) or 0.0)
                      for row in oi_hist]
            values = [v for v in values if v]
            if len(values) >= 2 and values[0]:
                info.open_interest_change_pct = (values[-1] / values[0] - 1) * 100
                info.available = True
        return info

    # ------------------------------------------------------------------
    # Контекст рынка
    # ------------------------------------------------------------------
    async def market_context(self, tickers: Optional[Dict[str, TickerInfo]] = None,
                             universe: Optional[Sequence[str]] = None) -> MarketContext:
        """
        Фон рынка: тренд BTC + ширина рынка.

        Используется как множитель для альт-сигналов: лонг в альткоине на
        падающем BTC — плохая идея, даже если по монете всё красиво.
        """
        tickers = tickers if tickers is not None else await self.tickers()
        ctx = MarketContext()
        if not tickers:
            ctx.notes.append("нет данных тикеров — контекст рынка не учтён")
            return ctx

        stats = self.universe_stats(tickers)
        ctx.breadth_24h_positive = stats.get("positive_ratio", 0.5)
        ctx.median_change_24h = stats.get("change_24h_median", 0.0)

        btc_symbol = f"BTC/{self.settings.quote}"
        btc_candles = await self.candles(btc_symbol, self.settings.signal_timeframe, 300)
        btc_daily = await self.candles(btc_symbol, Timeframe.D1, 120)
        if btc_candles is not None and not btc_candles.empty:
            score, trend, direction = _btc_trend(btc_candles, btc_daily)
            ctx.btc_score = score
            ctx.btc_trend = trend
            ctx.btc_direction = direction
        else:
            ctx.notes.append("нет свечей BTC — контекст по BTC не учтён")

        # Множитель для альтов: бычий BTC помогает лонгам, медвежий — шортам.
        # Направление сигнала неизвестно заранее, поэтому масштаб симметричный.
        ctx.alt_multiplier = float(max(0.65, min(1.15, 1.0 + abs(ctx.btc_score) * 0.15)))

        if ctx.breadth_24h_positive < 0.35:
            ctx.regime = "риск-офф (рынок под давлением)"
        elif ctx.breadth_24h_positive > 0.65:
            ctx.regime = "риск-он (широкий рост)"
        else:
            ctx.regime = "смешанный"
        return ctx


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _f(value) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _btc_trend(candles: Candles, daily: Optional[Candles]) -> Tuple[float, str, object]:
    """
    Быстрая оценка тренда BTC в [-1, 1] (для контекста, без полного анализа).

    Сигнальный ТФ даёт направление, дневки — режим (выше/ниже EMA50).
    """
    from app.domain.models import Direction

    close = candles.close
    ema20 = ind.last_valid(ind.ema(close, 20))
    ema50 = ind.last_valid(ind.ema(close, 50))
    slope = ind.last_valid(ind.linear_slope(close, 24, normalize=True))
    price = candles.last_price
    votes: List[float] = []
    if ema20 and ema50:
        votes.append(1.0 if ema20 > ema50 else -1.0)
    if price and ema50:
        votes.append(max(-1.0, min(1.0, (price / ema50 - 1) / 0.03)))
    if slope == slope:
        votes.append(max(-1.0, min(1.0, slope / 0.5)))
    if daily is not None and not daily.empty:
        d_ema50 = ind.last_valid(ind.ema(daily.close, 50))
        d_price = daily.last_price
        if d_ema50:
            votes.append(max(-1.0, min(1.0, (d_price / d_ema50 - 1) / 0.10)))
    if not votes:
        return 0.0, "нет данных", Direction.WAIT
    score = sum(votes) / len(votes)
    if score > 0.25:
        trend, direction = "восходящий", Direction.LONG
    elif score < -0.25:
        trend, direction = "нисходящий", Direction.SHORT
    else:
        trend, direction = "боковой", Direction.WAIT
    return float(max(-1.0, min(1.0, score))), trend, direction
