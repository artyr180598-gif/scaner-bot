"""
Сканер — оркестратор полного цикла.

1) Получить список топ-тикеров по объёму (exchange.gateway).
2) Для каждого тикера параллельно подтянуть свечи по N таймфреймам.
3) Прогнать индикаторы + групповые сигналы на базовом ТФ.
4) Посчитать скоринг (Scorer) и направление.
5) Проверить фильтры (Filters) — ликвидность, ATR, анти-гонка, confidence.
6) Построить SignalPlan (Planner).
7) Сгенерировать логику входа (Narrator).
8) Сформировать Signal, отдать в отчёт.

Вдохновлено Freqtrade (IStrategy.populate_indicators + entry/exit trend)
и OctoBot (Evaluators/Tentacles как независимые модули).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from ..analysis.filters import Filters
from ..analysis.plan import Planner
from ..analysis.scoring import Scorer
from ..config.settings import Settings
from ..core.domain.candle import Candles
from ..core.domain.market import MarketSnapshot, TickerMeta
from ..core.domain.signal import Confidences, Direction, Signal, SignalStatus
from ..core.domain.ticker import Ticker
from ..core.events import EventBus, ScanFinished, ScanStarted
from ..data.exchange.gateway import ExchangeGateway
from ..services.llm import Narrator

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    """Итог одного скана."""

    exchange: str
    scanned: int = 0
    candidates: int = 0
    signals: List[Signal] = field(default_factory=list)
    rejected: Dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0
    started_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "scanned": self.scanned,
            "candidates": self.candidates,
            "signals": [s.signal_id for s in self.signals],
            "rejected": self.rejected,
            "duration_seconds": round(self.duration_seconds, 2),
            "started_at": self.started_at,
        }


class ScannerEngine:
    """Главный оркестратор. Stateless: всё передаётся через init/метод."""

    def __init__(
        self,
        settings: Settings,
        gateway: ExchangeGateway,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.s = settings
        self.gw = gateway
        self.bus = event_bus or EventBus()
        self.scorer = Scorer(settings)
        self.filters = Filters(settings)
        self.planner = Planner()
        self.narrator = Narrator(settings)

    async def close(self) -> None:
        await self.narrator.close()

    # ── Public API ─────────────────────────────────────────────
    async def scan_exchange(
        self, exchange: str, top_n: Optional[int] = None
    ) -> ScanReport:
        """
        Полный скан одной биржи. Возвращает ScanReport с сигналами,
        прошедшими ВСЕ фильтры (включая min_confidence >= 75%).

        `top_n` — необязательный переопределяющий лимит тикеров для этого
        скана. Если не задан — берётся значение из настроек
        (settings.top_n_symbols). Settings frozen, поэтому лимит передаём
        параметром, а не мутируем настройки.
        """
        report = ScanReport(exchange=exchange, started_at=time.time())
        t0 = time.time()

        limit = self.s.top_n_symbols if top_n is None else top_n

        await self.bus.publish(ScanStarted(trigger="scan_exchange"))

        # 1) Тикеры
        tickers = await self.gw.fetch_tickers_meta(exchange, top_n=limit)
        report.scanned = len(tickers)
        log.info("[%s] fetched %d tickers", exchange, len(tickers))

        if not tickers:
            await self._finish(report, t0)
            return report

        # 2) Параллельно фильтруем pre-ticker (быстро отсекаем мусор).
        candidates: List[TickerMeta] = []
        for meta in tickers:
            r = self.filters.check_ticker(meta)
            if r.passed:
                candidates.append(meta)
            else:
                report.rejected[meta.symbol] = r.reason

        report.candidates = len(candidates)
        log.info("[%s] after ticker filters: %d", exchange, len(candidates))

        # 3) Параллельно тянем свечи по базовому ТФ.
        base_tf = self.s.base_timeframe
        sem = asyncio.Semaphore(8)  # не больше 8 параллельных запросов

        async def _fetch_one(meta: TickerMeta):
            async with sem:
                return meta, await self.gw.fetch_candles(
                    exchange, meta.symbol, base_tf, limit=self.s.candles_limit
                )

        results = await asyncio.gather(
            *(_fetch_one(m) for m in candidates), return_exceptions=True
        )

        for res in results:
            if isinstance(res, BaseException) or not isinstance(res, tuple):
                continue
            meta, candles = res
            if candles is None or len(candles) < 50:
                report.rejected[meta.symbol] = "fetch_failed"
                continue
            signal = await self._build_signal_for(meta, candles)
            if signal is not None:
                report.signals.append(signal)
            else:
                report.rejected[meta.symbol] = "no_signal"

        await self._finish(report, t0)
        return report

    async def scan_all(self, top_n: Optional[int] = None) -> Dict[str, ScanReport]:
        """
        Сканирует все биржи из gateway. Возвращает exchange → ScanReport.

        `top_n` — необязательный переопределяющий лимит тикеров (см.
        scan_exchange). Если не задан — используется settings.top_n_symbols.
        """
        out: Dict[str, ScanReport] = {}
        for ex in self.gw.available():
            try:
                out[ex] = await self.scan_exchange(ex, top_n=top_n)
            except Exception as exc:  # noqa: BLE001
                log.exception("scan %s failed: %s", ex, exc)
                out[ex] = ScanReport(exchange=ex, duration_seconds=0.0)
        return out

    # ── Internals ─────────────────────────────────────────────
    async def _build_signal_for(
        self, meta: TickerMeta, candles: Candles
    ) -> Optional[Signal]:
        """Полный пайплайн: df → скоринг → фильтры → план → нарратив → Signal."""
        df = candles.df
        if df.empty:
            return None

        # 1) Прогон индикаторов + групповые сигналы.
        df = self.scorer.enrich(df)

        # 2) Скоринг по группам.
        group_scores = self.scorer.score_groups(df)
        direction = self.scorer.decide_direction(group_scores)
        if direction == Direction.NEUTRAL:
            return None

        # 3) Confidence + фильтры.
        conf = self.scorer.build_confidences(df, group_scores, direction)
        ok = self.filters.all_pre_signal(meta, candles, df)
        if not ok.passed:
            return None
        ok2 = self.filters.check_min_confidence(conf.signal, conf.data)
        if not ok2.passed:
            return None

        # 4) План сделки.
        plan = self.planner.build(df, direction, meta.last_price)

        # 5) Старшие ТФ для контекста (можно использовать для подтверждения).
        try:
            older = await self.gw.fetch_multi_tf_candles(
                meta.exchange,
                meta.symbol,
                [tf for tf in self.s.analysis_timeframes if tf != self.s.base_timeframe],
                limit=self.s.candles_limit,
            )
            timeframes_used = list(older.keys()) + [self.s.base_timeframe]
        except Exception as exc:  # noqa: BLE001
            log.debug("multi-tf fetch failed for %s: %s", meta.symbol, exc)
            older = {}
            timeframes_used = [self.s.base_timeframe]

        # 6) Сигнал (черновик).
        signal = Signal(
            symbol=meta.symbol,
            exchange=meta.exchange,
            direction=direction,
            last_price=meta.last_price,
            timeframe_base=self.s.base_timeframe,
            timeframes_used=timeframes_used,
            confidences=conf,
            plan=plan,
            entry_logic="",  # заполним ниже
            logic_factors=[],
            signal_id=uuid.uuid4().hex,
            created_at=time.time(),
            status=SignalStatus.DRAFT,
        )

        # 7) Нарратив.
        narrative = await self.narrator.narrate(signal)
        signal.entry_logic = narrative.text
        signal.logic_factors = narrative.factors
        return signal

    async def _finish(self, report: ScanReport, t0: float) -> None:
        report.duration_seconds = time.time() - t0
        await self.bus.publish(
            ScanFinished(
                duration_seconds=report.duration_seconds,
                scanned=report.scanned,
                candidates=report.candidates,
                signals=[
                    {
                        "signal_id": s.signal_id,
                        "symbol": s.symbol,
                        "direction": s.direction.value,
                        "confidence": s.confidences.signal,
                    }
                    for s in report.signals
                ],
            )
        )
