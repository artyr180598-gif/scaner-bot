"""
Стратегия по умолчанию: «импульс в тренде».

Идея (из Freqtrade + CryptoSignal + OctoBot):
- Несколько независимых групп факторов (тренд, моментум, объём, …).
- Группы схлопываются в один голос (-1/0/+1), чтобы не пересчитывать
  одно и то же наблюдение много раз.
- Направление определяется балансом активных групп.
- Confidence — это СТЕПЕНЬ СОГЛАСИЯ групп, а НЕ вероятность прибыли.
"""
from __future__ import annotations

from typing import Dict, List

import pandas as pd

from ..core.domain.signal import Direction
from ..indicators import ALL_GROUPS, all_indicators, attach_signals
from .base import ACTIVE_GROUP_THRESHOLD, IStrategy


class MomentumTrendStrategy(IStrategy):
    name = "momentum_trend"
    description = (
        "Импульс в тренде: ловим момент, когда тренд, моментум и объём "
        "подтверждают друг друга, но актив ещё не перегрет."
    )

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return attach_signals(df, all_indicators())

    def group_scores(self, df: pd.DataFrame) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if df.empty:
            for g in ALL_GROUPS:
                out[g] = 0.0
            return out
        last = df.iloc[-1]
        for g in ALL_GROUPS:
            col = f"{g}_signal_raw"
            if col in df.columns:
                val = float(last[col]) if not pd.isna(last[col]) else 0.0
            else:
                val = 0.0
            out[g] = round(float(val), 4)
        return out

    def decide_direction(self, scores: Dict[str, float]) -> Direction:
        balance = 0.0
        for g, score in scores.items():
            if abs(score) >= ACTIVE_GROUP_THRESHOLD:
                balance += score
        if balance > 0.2:
            return Direction.LONG
        if balance < -0.2:
            return Direction.SHORT
        return Direction.NEUTRAL

    def confidence(self, scores: Dict[str, float], direction: Direction) -> float:
        """0..100 согласие активных групп. Никогда не 100."""
        sign = 1.0 if direction == Direction.LONG else -1.0
        active: List[str] = []
        for g, score in scores.items():
            if abs(score) >= ACTIVE_GROUP_THRESHOLD and score * sign > 0:
                active.append(g)

        if len(active) < 2:
            return 0.0

        num = 0.0
        den = 0.0
        for g in active:
            w = self.weights.get(g, 0.5)
            num += w * abs(scores[g])
            den += w
        if den == 0:
            return 0.0
        avg = num / den
        breadth = min(1.0, len(active) / 5.0)
        score = (0.7 * avg + 0.3 * breadth) * 100
        return round(min(95.0, max(0.0, score)), 1)


def build_strategy(name: str = "momentum_trend") -> IStrategy:
    from .registry import get_strategy
    return get_strategy(name)
