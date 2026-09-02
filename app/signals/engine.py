"""
app/signals/engine.py — движок анализа: снимок рынка → сигнал.

Конвейер (одна монета):
    MarketSnapshot
      → build_features      (индикаторы, структура, уровни, метрики)
      → run_modules         (факторы от всех модулей анализа)
      → score_factors       (направление, счёт, уверенность, потенциал)
      → build_plan          (вход/стоп/цели/R:R)
      → apply_filters       (гейты качества)
      → Signal              (+ объяснение на русском)

Движок ничего не знает про Telegram и сеть — его можно гонять в бектесте,
в CLI и в unit-тестах на синтетике.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence

from app.analysis.base import AnalysisModule, MarketFeatures, run_modules
from app.analysis.features import build_features
from app.analysis.registry import all_modules
from app.config.settings import Settings
from app.domain.models import (Direction, FactorSet, MarketContext, MarketSnapshot,
                               Signal, Timeframe)
from app.scoring.scorer import ScoreResult, score_factors
from app.signals.explain import build_summary, detect_setup
from app.signals.filters import FilterConfig, FilterResult, apply_filters
from app.signals.planner import PlanConfig, build_plan, plan_from_config

log = logging.getLogger(__name__)


class SignalEngine:
    """Ядро продукта: превращает данные рынка в сигнал с планом сделки."""

    def __init__(
        self,
        settings: Settings,
        modules: Optional[Sequence[AnalysisModule]] = None,
        plan_config: Optional[PlanConfig] = None,
        filter_config: Optional[FilterConfig] = None,
    ) -> None:
        self.settings = settings
        self.modules: List[AnalysisModule] = list(modules or all_modules())
        self.plan_config = plan_config or plan_from_config(settings)
        # Свежесть: последняя ЗАКРЫТАЯ свеча часового ТФ закрылась не позже часа
        # назад — это норма, а не «устаревшие данные». Поэтому допуск не может
        # быть меньше 1.5 периода сигнального таймфрейма, иначе на базовом ТФ 1h
        # отвергался бы каждый сигнал.
        tf_seconds = settings.signal_timeframe.minutes * 60
        self.filter_config = filter_config or FilterConfig(
            min_confidence=settings.min_confidence,
            min_rr=settings.min_rr,
            min_agreement=2,
            anti_chase_atr=settings.anti_chase_atr,
            min_quote_volume_usd=settings.min_quote_volume_usd,
            max_staleness_seconds=max(settings.max_staleness_seconds,
                                      int(tf_seconds * 1.5)),
        )

    # ------------------------------------------------------------------
    def build_features(
        self,
        snapshot: MarketSnapshot,
        context: Optional[MarketContext] = None,
    ) -> MarketFeatures:
        return build_features(
            snapshot,
            signal_tf=self.settings.signal_timeframe,
            context=context,
        )

    # ------------------------------------------------------------------
    def analyze(
        self,
        snapshot: MarketSnapshot,
        context: Optional[MarketContext] = None,
        features: Optional[MarketFeatures] = None,
    ) -> Signal:
        """Полный анализ одной монеты. Всегда возвращает Signal (возможно WAIT)."""
        if features is None:
            features = self.build_features(snapshot, context)

        factor_set = FactorSet(
            factors=list(run_modules(self.modules, features)),
            data_confidence=data_confidence(features),
            notes=list(features.notes),
        )
        score = score_factors(
            factor_set, wait_threshold=self.settings.wait_threshold)

        plan = build_plan(features, score.direction, self.plan_config)
        verdict = apply_filters(score, plan, features, self.filter_config)

        direction = score.direction if verdict.accepted else Direction.WAIT
        final_plan = plan if verdict.accepted else None
        summary = build_summary(features, score, final_plan)
        if not verdict.accepted and verdict.reasons:
            summary += "\n\n🚫 Почему не сигнал: " + "; ".join(verdict.reasons[:3])

        return Signal(
            symbol=snapshot.symbol,
            direction=direction,
            confidence=round(score.confidence, 2),
            score=round(score.score, 4),
            timeframe=features.signal_tf,
            plan=final_plan,
            factors=factor_set,
            summary=summary,
            setup=detect_setup(features, score) if score.direction is not Direction.WAIT
            else "наблюдение",
            exchange=snapshot.exchange,
            price=snapshot.price,
            tags=self._tags(features, score, verdict),
            data_confidence=round(factor_set.data_confidence, 3),
            horizon_hours=self.plan_config.horizon_hours,
            metrics=features.metrics,
        )

    # ------------------------------------------------------------------
    def analyze_many(
        self,
        snapshots: Iterable[MarketSnapshot],
        context: Optional[MarketContext] = None,
    ) -> List[Signal]:
        """Последовательный анализ списка (для тестов и небольших выборок)."""
        out: List[Signal] = []
        for snapshot in snapshots:
            try:
                out.append(self.analyze(snapshot, context))
            except Exception as exc:  # noqa: BLE001
                log.exception("%s: анализ упал: %s", snapshot.symbol, exc)
        return out

    # ------------------------------------------------------------------
    def _tags(self, features: MarketFeatures, score: ScoreResult,
              verdict: FilterResult) -> List[str]:
        tags: List[str] = []
        if features.flags.get("compressed"):
            tags.append("сжатие")
        if features.flags.get("volume_spike"):
            tags.append("аномалия объёма")
        if features.flags.get("already_ran"):
            tags.append("уже росла")
        if features.flags.get("at_support"):
            tags.append("у поддержки")
        if features.flags.get("at_resistance"):
            tags.append("у сопротивления")
        if score.potential >= 0.65:
            tags.append("высокий потенциал")
        if not verdict.hard:
            tags.append("ждать откат")
        if features.derivatives.funding_pct is not None:
            tags.append("есть перп")
        return tags[:6]


# ---------------------------------------------------------------------------
# Качество данных
# ---------------------------------------------------------------------------

def data_confidence(features: MarketFeatures) -> float:
    """
    Отдельная «уверенность в данных» (0..1).

    Принципиально отделена от уверенности в идее: сигнал на протухших свечах
    может выглядеть сильным, но доверять ему нельзя.
    """
    score = 1.0
    primary = features.primary()
    bars = primary.bars
    if bars < 120:
        score -= 0.45
    elif bars < 200:
        score -= 0.25
    elif bars < 300:
        score -= 0.10

    staleness = features.snapshot.staleness_seconds()
    tf_seconds = max(60.0, features.signal_tf.minutes * 60)
    if staleness > 3 * tf_seconds:
        score -= 0.4
    elif staleness > 1.5 * tf_seconds:
        score -= 0.2

    ticker = features.ticker
    if ticker is None:
        score -= 0.15
    else:
        if ticker.quote_volume <= 0:
            score -= 0.3
        elif ticker.quote_volume < 500_000:
            score -= 0.2

    if features.snapshot.warnings:
        score -= min(0.3, 0.1 * len(features.snapshot.warnings))
    if len(features.indicators) < 2:
        score -= 0.1      # нет мульти-таймфреймового подтверждения
    return float(max(0.0, min(1.0, score)))
