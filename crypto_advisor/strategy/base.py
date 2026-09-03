"""
Базовый класс стратегии — Freqtrade-inspired.

Freqtrade строит стратегию как класс с методами:
    populate_indicators(df)        # добавляем индикаторы
    populate_entry_trend(df)       # условия входа (-> 1 possible entry)
    populate_exit_trend(df)        # условия выхода

Мы упрощаем до:
    name
    populate_indicators(df) -> df          # индикаторы + групповые сигналы
    group_scores(df) -> dict[str, float]   # -1..+1 по факторам
    decide_direction(scores) -> Direction
    confidence(scores, direction) -> float # 0..100 согласие факторов
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Dict, List

import pandas as pd

from ..core.domain.signal import Direction

log = logging.getLogger(__name__)

ACTIVE_GROUP_THRESHOLD = 0.10


class IStrategy(ABC):
    """Интерфейс стратегии. Наследники реализуют 4 метода."""

    name: str = "base"
    description: str = ""
    # Группы факторов, которые участвуют в решении (порядок важен для объяснения).
    groups: tuple = ("trend", "momentum", "volume", "volatility", "structure", "patterns")
    # Веса групп (влияют на confidence).
    weights: Dict[str, float] = {
        "trend": 1.0,
        "momentum": 0.9,
        "volume": 0.8,
        "volatility": 0.6,
        "structure": 0.7,
        "patterns": 0.5,
    }

    @abstractmethod
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Возвращает df с индикаторами и групповыми сигналами."""

    @abstractmethod
    def group_scores(self, df: pd.DataFrame) -> Dict[str, float]:
        """Возвращает -1..+1 по каждой группе (по последнему бару)."""

    @abstractmethod
    def decide_direction(self, scores: Dict[str, float]) -> Direction:
        """Long/Short/Neutral по согласованию групп."""

    @abstractmethod
    def confidence(self, scores: Dict[str, float], direction: Direction) -> float:
        """0..100 — степень согласия факторов в выбранном направлении."""

    def active_groups(self, scores: Dict[str, float], direction: Direction) -> List[str]:
        """Группы, которые «голосуют» в сторону direction."""
        sign = 1.0 if direction == Direction.LONG else -1.0
        out: List[str] = []
        for g, score in scores.items():
            if abs(score) >= ACTIVE_GROUP_THRESHOLD and score * sign > 0:
                out.append(g)
        return out

    def contributing_indicators(self, df: pd.DataFrame, group: str) -> List[str]:
        """Имена индикаторов группы, которые не молчат на последнем баре."""
        from ..indicators import all_indicators
        out: List[str] = []
        for ind in all_indicators():
            if ind.group != group:
                continue
            sig = ind.signal(df)
            if len(sig.series) and abs(float(sig.series.iloc[-1])) > 0:
                out.append(ind.name)
        return out
