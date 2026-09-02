"""
app/scoring/scorer.py — свёртка факторов в направление, счёт и уверенность.

Порядок расчёта:
  1. факторы → оценка группы (взвешенное среднее внутри группы);
  2. группы → направленный счёт ``score`` ∈ [-1, 1] (взвешенное среднее групп);
  3. считаем число СОГЛАСНЫХ групп → потолок уверенности;
  4. потенциал движения (ненаправленные факторы) масштабирует уверенность;
  5. качество данных/ликвидности режет уверенность.

Результат — ``ScoreResult``: его едят движок сигналов и рендерер.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.analysis.base import DIRECTIONAL_GROUPS, Group
from app.domain.models import Direction, Factor, FactorSet
from app.scoring.weights import (AGREEMENT_THRESHOLD, CONFIDENCE_FULL_SCORE,
                                 GROUP_WEIGHTS, MIN_AGREEMENT, OPINION_THRESHOLD,
                                 POTENTIAL_INFLUENCE, QUALITY_FLOOR, confidence_cap)


@dataclass(slots=True)
class GroupScore:
    group: str
    score: float            # -1..1 (для POTENTIAL: 0..1)
    weight: float
    n_factors: int
    evidence: List[str] = field(default_factory=list)

    @property
    def directional(self) -> bool:
        return self.group in DIRECTIONAL_GROUPS


@dataclass(slots=True)
class ScoreResult:
    direction: Direction
    score: float                     # -1..1
    confidence: float                # 0..10
    potential: float                 # 0..1 — вероятность сильного движения
    data_confidence: float           # 0..1 — качество данных
    quality: float                   # 0..1 — штрафы за данные/ликвидность
    agreement: int                   # число согласных групп
    groups: Dict[str, GroupScore] = field(default_factory=dict)
    bullish: List[str] = field(default_factory=list)
    bearish: List[str] = field(default_factory=list)
    capped: bool = False
    notes: List[str] = field(default_factory=list)

    def group_score(self, group: str) -> float:
        g = self.groups.get(group)
        return g.score if g else 0.0

    def top_evidence(self, n: int = 4) -> List[str]:
        """Самые весомые аргументы в пользу выбранного направления."""
        pool = self.bullish if self.direction is Direction.LONG else self.bearish
        return pool[:n]

    def risks(self, n: int = 3) -> List[str]:
        """Главные контр-аргументы (честность: показываем и их)."""
        pool = self.bearish if self.direction is Direction.LONG else self.bullish
        return pool[:n]


# ---------------------------------------------------------------------------
# Шаг 1: факторы → группы
# ---------------------------------------------------------------------------

def collapse_groups(factor_set: FactorSet) -> Dict[str, GroupScore]:
    """Сворачивает зависимые факторы в одну оценку группы."""
    out: Dict[str, GroupScore] = {}
    for group, factors in factor_set.by_group().items():
        total_weight = sum(f.weight for f in factors) or float(len(factors))
        if total_weight <= 0:
            continue
        score = sum(f.score * f.weight for f in factors) / total_weight
        # Для ненаправленных групп берём модуль: они не голосуют за сторону.
        if group not in DIRECTIONAL_GROUPS:
            score = sum(abs(f.score) * f.weight for f in factors) / total_weight
        evidence = sorted(factors, key=lambda f: -abs(f.score) * f.weight)
        out[group] = GroupScore(
            group=group,
            score=float(max(-1.0, min(1.0, score))),
            weight=float(total_weight),
            n_factors=len(factors),
            evidence=[f.evidence for f in evidence if f.evidence],
        )
    return out


# ---------------------------------------------------------------------------
# Шаг 2: группы → счёт и уверенность
# ---------------------------------------------------------------------------

def score_factors(
    factor_set: FactorSet,
    *,
    wait_threshold: float = 0.12,
    group_weights: Optional[Dict[str, float]] = None,
) -> ScoreResult:
    """Основная функция скоринга. Чистая, детерминированная, тестируемая."""
    weights = group_weights or GROUP_WEIGHTS
    groups = collapse_groups(factor_set)

    directional = [g for g in groups.values() if g.directional]
    if not directional:
        return ScoreResult(
            direction=Direction.WAIT, score=0.0, confidence=0.0, potential=0.0,
            data_confidence=factor_set.data_confidence, quality=1.0, agreement=0,
            groups=groups, notes=["нет направленных аргументов"],
        )

    # Группы БЕЗ мнения (|score| < OPINION_THRESHOLD) не участвуют в знаменателе:
    # иначе девять групп, из которых четыре молчат, размазывают счёт до шума и
    # сигнал никогда не дотягивает до порога уверенности.
    opinionated = [g for g in directional if abs(g.score) >= OPINION_THRESHOLD]
    voters = opinionated or directional
    num = sum(g.score * weights.get(g.group, 0.6) for g in voters)
    den = sum(weights.get(g.group, 0.6) for g in voters)
    score = num / den if den else 0.0
    score = float(max(-1.0, min(1.0, score)))

    if score > 0:
        direction = Direction.LONG
    elif score < 0:
        direction = Direction.SHORT
    else:
        direction = Direction.WAIT

    # Согласные группы: знак совпал И вклад значим.
    sign = direction.sign
    agreeing = [g for g in directional if g.score * sign >= AGREEMENT_THRESHOLD]
    agreement = len(agreeing)

    # Потенциал движения (ненаправленные факторы).
    potential_groups = [g for g in groups.values() if g.group == Group.POTENTIAL]
    if potential_groups:
        potential = sum(g.score * g.weight for g in potential_groups) / \
            sum(g.weight for g in potential_groups)
    else:
        potential = 0.5          # нейтральное допущение, если модуль не отработал
    potential = float(max(0.0, min(1.0, potential)))

    # Качество: сумма отрицательных факторов QUALITY.
    quality_groups = [g for g in groups.values() if g.group == Group.QUALITY]
    if quality_groups:
        penalty = sum(abs(g.score) * g.weight for g in quality_groups) / \
            sum(g.weight for g in quality_groups)
        quality = float(max(QUALITY_FLOOR, min(1.0, 1.0 - penalty)))
    else:
        quality = 1.0

    # --- уверенность -------------------------------------------------------
    base = min(1.0, abs(score) / CONFIDENCE_FULL_SCORE) * 10.0
    cap = confidence_cap(agreement)
    capped = base > cap
    confidence = min(base, cap)

    lo, hi = POTENTIAL_INFLUENCE
    confidence *= lo + (hi - lo) * potential
    confidence *= quality
    confidence *= 0.7 + 0.3 * factor_set.data_confidence
    confidence = float(max(0.0, min(10.0, confidence)))

    # Слабый счёт или рассогласование групп → WAIT.
    notes: List[str] = []
    if abs(score) < wait_threshold:
        direction = Direction.WAIT
        notes.append(f"счёт {score:+.2f} слишком слаб: аргументы противоречат друг другу")
    if agreement < MIN_AGREEMENT and direction is not Direction.WAIT:
        notes.append(
            f"направление поддерживает только {agreement} независимая группа — "
            f"сигнал не публикуется")
        direction = Direction.WAIT

    bullish, bearish = _split_evidence(groups, direction)
    return ScoreResult(
        direction=direction, score=score, confidence=confidence,
        potential=potential, data_confidence=factor_set.data_confidence,
        quality=quality, agreement=agreement, groups=groups,
        bullish=bullish, bearish=bearish, capped=capped,
        notes=notes + list(factor_set.notes),
    )


def _split_evidence(groups: Dict[str, GroupScore],
                    direction: Direction) -> tuple[List[str], List[str]]:
    """Разбирает объяснения на «за лонг» и «за шорт» (для блока «Почему»)."""
    bullish: List[tuple[float, str]] = []
    bearish: List[tuple[float, str]] = []
    for g in groups.values():
        if not g.directional or not g.evidence:
            continue
        weight = GROUP_WEIGHTS.get(g.group, 0.6)
        for text in g.evidence:
            if g.score > 0:
                bullish.append((g.score * weight, text))
            elif g.score < 0:
                bearish.append((abs(g.score) * weight, text))
    bullish.sort(key=lambda x: -x[0])
    bearish.sort(key=lambda x: -x[0])
    return [t for _, t in bullish], [t for _, t in bearish]


def confidence_label(confidence: float) -> str:
    """Человеческая шкала уверенности (для интерфейса)."""
    if confidence >= 8.5:
        return "очень высокая"
    if confidence >= 7.0:
        return "высокая"
    if confidence >= 5.5:
        return "средняя"
    if confidence >= 4.0:
        return "низкая"
    return "очень низкая"


def potential_label(potential: float) -> str:
    if potential >= 0.75:
        return "высокий потенциал движения"
    if potential >= 0.5:
        return "средний потенциал движения"
    if potential >= 0.3:
        return "низкий потенциал движения"
    return "монета «спит» — движения можно не дождаться"


def finite_or(value: Optional[float], default: float = float("nan")) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default
    return float(value)
