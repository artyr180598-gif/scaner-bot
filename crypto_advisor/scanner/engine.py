"""
ScannerEngine — оркестратор полного цикла.

1) Получить список топ-тикеров по объёму (exchange gateway).
2) Для каждого тикера параллельно подтянуть свечи.
3) Прогнать индикаторы + группы (strategy).
4) Matcher подбирает монеты под UserRequest и ранжирует.
5) Для лучших — Planner строит план, Explainer генерирует «почему».
6) Формируется Signal (совет Long/Short + причина + уверенность).

Вдохновлено Freqtrade (populate_indicators + entry/exit trend),
Jesse (чистый research workflow) и OctoBot (модульные evaluators).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from ..analysis.explainer import Explainer
from ..analysis.matcher import Match, Matcher
from ..analysis.planner import Planner
from ..config.settings import Settings
from ..core.domain.query import UserRequest
from ..core.domain.signal import Direction, Signal
from ..data.exchange import ExchangeGateway, SyntheticExchange, TickerMeta
from ..strategy import get_strategy
from ..strategy.base import IStrategy

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ScanResult:
    """Итог подбора монет под запрос."""

    request: UserRequest
    matches: List[Match] = field(default_factory=list)
    scanned: int = 0
    rejected: int = 0
    duration_seconds: float = 0.0
    exchange: str = ""

    @property
    def top(self) -> List[Match]:
        return [m for m in self.matches if not m.rejected_reason]


class ScannerEngine:
    """Stateless: всё передаётся через init/метод."""

    def __init__(self, settings: Settings, gateway, strategy: Optional[IStrategy] = None) -> None:
        self.s = settings
        self.gw = gateway
        self.strategy = strategy or get_strategy("momentum_trend")
        self.matcher = Matcher(self.strategy)
        self.planner = Planner()
        self.explainer = Explainer(settings)

    async def close(self) -> None:
        await self.explainer.close()

    # ── Public API ─────────────────────────────────────────────
    async def find_matches(
        self, request: UserRequest, top_n: Optional[int] = None
    ) -> ScanResult:
        """Подбирает монеты под запрос. Возвращает ScanResult с ранжированными Match."""
        t0 = time.time()
        result = ScanResult(request=request)

        limit = self.s.top_n_symbols if top_n is None else top_n
        exchange = self._pick_exchange()
        result.exchange = exchange

        tickers = await self._fetch_tickers(exchange, limit)
        result.scanned = len(tickers)
        log.info("[%s] fetched %d tickers", exchange, len(tickers))

        sem = asyncio.Semaphore(8)

        async def _one(meta: TickerMeta):
            async with sem:
                candles = await self._fetch_candles(exchange, meta.symbol, request.timeframe)
                if candles is None or len(candles) < 60:
                    return None
                return self.matcher.match(meta, candles.df, request)

        matched = await asyncio.gather(*(_one(m) for m in tickers), return_exceptions=True)
        matches: List[Match] = []
        for res in matched:
            if isinstance(res, BaseException) or res is None:
                result.rejected += 1
                continue
            if res.rejected_reason:
                result.rejected += 1
            matches.append(res)

        # Ранжируем: non-rejected по match_score, затем rejected (для справки).
        matches.sort(key=lambda m: (bool(m.rejected_reason), -m.match_score))
        result.matches = matches[: max(request.limit, self.s.max_matches)]
        result.duration_seconds = time.time() - t0
        return result

    async def analyze_symbol(self, exchange: str, symbol: str, request: UserRequest) -> Optional[Signal]:
        """Глубокий анализ конкретной монеты под запрос.

        Тянет РЕАЛЬНЫЕ метаданные тикера (цена, объём, изменение за 24ч),
        если gateway умеет, иначе запасной вариант — по последней свече.
        """
        candles = await self._fetch_candles(exchange, symbol, request.timeframe)
        if candles is None or len(candles) < 60:
            return None

        meta = None
        if hasattr(self.gw, "fetch_ticker_meta"):
            meta = await self.gw.fetch_ticker_meta(exchange, symbol)

        last_price = (meta.last_price if meta else
                      float(candles.last.close) if candles.last else 0.0)
        if meta is None:
            meta = TickerMeta(
                symbol=symbol, exchange=exchange, last_price=last_price,
                quote_volume_24h=0.0, change_pct_24h=0.0,
            )

        match = self.matcher.match(meta, candles.df, request, deep=True)
        if match is None or match.rejected_reason:
            return None
        return await self._build_signal(meta, candles.df, request, match)

    # ── Internals ─────────────────────────────────────────────
    def _pick_exchange(self) -> str:
        if hasattr(self.gw, "available"):
            avail = self.gw.available()
            return avail[0] if avail else "binance"
        return getattr(self.gw, "exchange", "synthetic")

    async def _fetch_tickers(self, exchange: str, top_n: int) -> List[TickerMeta]:
        if hasattr(self.gw, "fetch_tickers_meta"):
            return await self.gw.fetch_tickers_meta(exchange, top_n=top_n)
        return []

    async def _fetch_candles(self, exchange: str, symbol: str, timeframe: str):
        if hasattr(self.gw, "fetch_candles"):
            return await self.gw.fetch_candles(exchange, symbol, timeframe, limit=self.s.candles_limit)
        return None

    async def _build_signal(self, meta: TickerMeta, df: pd.DataFrame, request: UserRequest, match: Match) -> Signal:
        """Собирает Signal из Match + плана + объяснения."""
        # Направление и уверенность уже посчитаны в Match.
        df_enriched = self.strategy.populate_indicators(df) if "trend_signal_raw" not in df.columns else df

        # Старшие ТФ для контекста (best-effort, не критично).
        timeframes_used = [request.timeframe]
        if hasattr(self.gw, "fetch_candles"):
            for tf in [t for t in self.s.analysis_timeframes if t != request.timeframe]:
                c = await self._fetch_candles(meta.exchange, meta.symbol, tf)
                if c is not None:
                    timeframes_used.append(tf)

        plan = self.planner.build(df_enriched, match.direction, meta.last_price)
        signal = Signal(
            symbol=meta.symbol,
            exchange=meta.exchange,
            direction=match.direction,
            last_price=meta.last_price,
            change_24h=meta.change_pct_24h,
            volume_24h=meta.quote_volume_24h,
            timeframe_base=request.timeframe,
            timeframes_used=timeframes_used,
            confidences=self._make_confidences(match),
            plan=plan,
            reason="", reason_factors=[],
            signal_id=uuid.uuid4().hex,
            created_at=time.time(),
        )

        explanation = await self.explainer.explain(signal, request)
        signal.reason = explanation.text
        signal.reason_factors = explanation.factors
        return signal

    def _make_confidences(self, match: Match):
        from ..analysis.scorer import build_confidences
        from ..core.domain.signal import Confidences
        return Confidences(
            data=match.data_confidence,
            signal=match.signal_confidence,
            risk_profile=match.reasons and "balanced" or "balanced",
            group_caps=match.group_scores,
        )
