"""
Скоринг — превращаем «-1/0/+1» от каждой группы в финальную уверенность.

Уроки BRAIN.md:
- п.13: среднее по всем группам убивает сигнал — группы с |score| < 0.10 не идут
  в знаменатель.
- п.0d: группы — это «независимые голоса»; повторный тренд на разных ТФ —
  одно наблюдение, а не два.
- п.0b: «уверенность прогноза» НЕ равно вероятности profit. Поэтому в UI
  помечаем это явно, а в формуле используем схлопывание по группам.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from ...config.settings import Settings
from ...core.domain.signal import Confidences, Direction
from ...indicators import ALL_GROUPS, attach_signals, all_indicators
from ...indicators.base import group_signal

log = logging.getLogger(__name__)

# Минимальный |score|, чтобы группа участвовала в усреднении.
ACTIVE_GROUP_THRESHOLD = 0.10

# Какие группы обязательны для сигнала (минимум 2 из них активны).
REQUIRED_GROUPS = ("trend", "momentum", "volume")


@dataclass
class GroupScore:
    group: str
    score: float       # в -1..+1
    contributing: List[str] = field(default_factory=list)  # имена индикаторов

    @property
    def is_active(self) -> bool:
        return abs(self.score) >= ACTIVE_GROUP_THRESHOLD


class Scorer:
    """Скоринг одной (symbol, timeframe) матрицы."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._indicators = all_indicators()

    def enrich(self, df: pd.DataFrame) -> pd.DataFrame:
        """Добавляет индикаторы + групповые сигналы в df."""
        return attach_signals(df, self._indicators)

    def score_data(self, df: pd.DataFrame) -> float:
        """
        Data Confidence: насколько полные/свежие данные (0..100).

        Учитывает:
        - длину истории (>= 100 баров = 100%).
        - % не-NaN значений.
        """
        if len(df) < 10:
            return 0.0
        length_score = min(1.0, len(df) / 100.0) * 100
        non_nan = df.notna().mean().mean() * 100
        return round(min(length_score, non_nan), 1)

    def score_groups(self, df: pd.DataFrame) -> Dict[str, GroupScore]:
        """
        Возвращает score для каждой группы. Учитывает только последний бар.
        """
        if df.empty:
            return {g: GroupScore(group=g, score=0.0) for g in ALL_GROUPS}

        last = df.iloc[-1]
        result: Dict[str, GroupScore] = {}
        for g in ALL_GROUPS:
            col = f"{g}_signal_raw"
            contributing = []
            if col in df.columns:
                val = float(last[col]) if not pd.isna(last[col]) else 0.0
            else:
                val = 0.0
            # Соберём имена индикаторов, чьи последние сигналы не нулевые.
            for ind in self._indicators:
                if ind.group != g:
                    continue
                sig = ind.signal(df)
                last_sig = float(sig.series.iloc[-1]) if len(sig.series) else 0.0
                if abs(last_sig) > 0:
                    contributing.append(ind.name)
            result[g] = GroupScore(group=g, score=float(val), contributing=contributing)
        return result

    def aggregate_confidence(
        self, group_scores: Dict[str, GroupScore], direction: Direction
    ) -> float:
        """
        Агрегирует активные группы в одну Signal Confidence (0..100).

        Правила:
        - Берём ТОЛЬКО группы, чей score совпадает по знаку с direction.
        - Если активных групп < 2 — сигнал слабый, штраф.
        - Каждая группа взвешена из settings.weights_*.
        - Финал ограничен сверху 95% (никогда не выдаём «100%»).
        """
        sign = 1.0 if direction == Direction.LONG else -1.0

        active: List[GroupScore] = []
        for g, gs in group_scores.items():
            if not gs.is_active:
                continue
            if gs.score * sign <= 0:
                continue
            active.append(gs)

        if len(active) < 2:
            return 0.0  # требуем минимум 2 согласные группы (урок 0d)

        # Вес из настроек.
        weight_map = {
            "trend": self.settings.weights_trend,
            "momentum": self.settings.weights_momentum,
            "volume": self.settings.weights_volume,
            "volatility": self.settings.weights_volatility,
            "structure": self.settings.weights_structure,
            "patterns": self.settings.weights_patterns,
        }

        num = 0.0
        den = 0.0
        for gs in active:
            w = weight_map.get(gs.group, 0.5)
            num += w * abs(gs.score)
            den += w

        if den == 0:
            return 0.0

        avg = num / den  # 0..1
        # Бонус за количество согласных групп (2 группы — базовый, 5+ — топ).
        breadth = min(1.0, len(active) / 5.0)
        score = (0.7 * avg + 0.3 * breadth) * 100
        return round(min(95.0, max(0.0, score)), 1)

    def decide_direction(self, group_scores: Dict[str, GroupScore]) -> Direction:
        """
        Решает направление (Long/Short/Neutral) по группам.
        Long побеждает, если сумма sign-weighted активных групп > 0.
        """
        balance = 0.0
        for gs in group_scores.values():
            if gs.is_active:
                balance += gs.score
        if balance > 0.2:
            return Direction.LONG
        if balance < -0.2:
            return Direction.SHORT
        return Direction.NEUTRAL

    def build_confidences(
        self, df: pd.DataFrame, group_scores: Dict[str, GroupScore], direction: Direction
    ) -> Confidences:
        """Собирает Confidences (data + signal)."""
        data = self.score_data(df)
        signal_c = self.aggregate_confidence(group_scores, direction)
        return Confidences(
            data=data,
            signal=signal_c,
            risk_profile="balanced",
            caps={g: round(gs.score, 3) for g, gs in group_scores.items()},
        )
