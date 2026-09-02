"""
tests/test_scoring.py — свёртка факторов в направление, счёт и уверенность.

Это ядро «честности» продукта: здесь проверяются правила, которые проект
вывел на реальных данных (см. AI_AGENTS/BRAIN.md):
  * зависимые факторы схлопываются в группу и не голосуют по отдельности;
  * уверенность ограничена числом СОГЛАСНЫХ независимых групп;
  * противоречивые аргументы дают WAIT, а не слабый сигнал;
  * потенциал и качество данных влияют на уверенность, но не на направление.
"""

from __future__ import annotations

import pytest

from app.analysis.base import Group
from app.domain.models import Direction, Factor, FactorSet
from app.scoring.scorer import (collapse_groups, confidence_label, potential_label,
                                score_factors)
from app.scoring.weights import GROUP_CONFIDENCE_CAPS


def f(name: str, group: str, score: float, weight: float = 1.0,
      evidence: str = "факт") -> Factor:
    return Factor(name=name, group=group, score=score, evidence=evidence, weight=weight)


# ---------------------------------------------------------------------------
# Схлопывание групп
# ---------------------------------------------------------------------------

def test_dependent_factors_collapse_into_one_group_score():
    """Три фактора тренда = ОДИН голос группы, а не три."""
    fs = FactorSet(factors=[
        f("t1", Group.TREND, 0.8), f("t2", Group.TREND, 0.9), f("t3", Group.TREND, 0.7),
        f("m1", Group.MOMENTUM, -0.6),
    ])
    groups = collapse_groups(fs)
    assert groups[Group.TREND].score == pytest.approx(0.8)
    assert groups[Group.TREND].n_factors == 3
    assert groups[Group.MOMENTUM].score == pytest.approx(-0.6)


def test_weighted_average_inside_group():
    fs = FactorSet(factors=[
        f("a", Group.MOMENTUM, 1.0, weight=3.0),
        f("b", Group.MOMENTUM, -1.0, weight=1.0),
    ])
    groups = collapse_groups(fs)
    # (1*3 + (-1)*1) / 4 = 0.5
    assert groups[Group.MOMENTUM].score == pytest.approx(0.5)


def test_nondirectional_groups_use_magnitude():
    fs = FactorSet(factors=[f("p", Group.POTENTIAL, 0.7), f("p2", Group.POTENTIAL, 0.3)])
    groups = collapse_groups(fs)
    assert groups[Group.POTENTIAL].score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Направление и счёт
# ---------------------------------------------------------------------------

def test_direction_follows_majority_of_groups():
    fs = FactorSet(factors=[
        f("t", Group.TREND, 0.9), f("s", Group.STRUCTURE, 0.8),
        f("m", Group.MOMENTUM, 0.6), f("v", Group.VOLUME, 0.4),
    ])
    res = score_factors(fs)
    assert res.direction is Direction.LONG
    assert res.score > 0.5


def test_short_direction():
    fs = FactorSet(factors=[
        f("t", Group.TREND, -0.9), f("s", Group.STRUCTURE, -0.8),
        f("m", Group.MOMENTUM, -0.5),
    ])
    res = score_factors(fs)
    assert res.direction is Direction.SHORT
    assert res.score < -0.4


def test_contradictory_factors_give_wait():
    fs = FactorSet(factors=[
        f("t", Group.TREND, 0.9), f("s", Group.STRUCTURE, -0.9),
        f("m", Group.MOMENTUM, 0.85), f("v", Group.VOLUME, -0.85),
    ])
    res = score_factors(fs)
    assert res.direction is Direction.WAIT
    assert abs(res.score) < 0.35


def test_empty_factor_set_gives_wait():
    res = score_factors(FactorSet())
    assert res.direction is Direction.WAIT
    assert res.confidence == 0.0


def test_wait_threshold_respected():
    fs = FactorSet(factors=[f("t", Group.TREND, 0.05), f("s", Group.STRUCTURE, 0.04)])
    res = score_factors(fs, wait_threshold=0.12)
    assert res.direction is Direction.WAIT


def test_single_group_cannot_produce_signal():
    """Одна согласная группа → WAIT (нужно минимум две независимые)."""
    fs = FactorSet(factors=[f("t", Group.TREND, 0.95)])
    res = score_factors(fs)
    assert res.direction is Direction.WAIT
    assert res.agreement == 1
    assert any("независим" in n for n in res.notes)


# ---------------------------------------------------------------------------
# Потолок уверенности
# ---------------------------------------------------------------------------

def test_confidence_capped_by_agreement():
    """Даже при счёте 1.0 уверенность не выше потолка для одной группы."""
    fs = FactorSet(factors=[
        f("t1", Group.TREND, 1.0), f("t2", Group.TREND, 1.0), f("t3", Group.TREND, 1.0),
    ])
    res = score_factors(fs)
    assert res.agreement == 1
    assert res.confidence <= GROUP_CONFIDENCE_CAPS[1]


def test_more_agreeing_groups_allow_higher_confidence():
    def build(n_groups: int) -> FactorSet:
        groups = [Group.TREND, Group.STRUCTURE, Group.MOMENTUM, Group.SMC,
                  Group.VOLUME, Group.LEVELS]
        return FactorSet(factors=[f(f"x{i}", groups[i], 0.9) for i in range(n_groups)])

    conf = [score_factors(build(n)).confidence for n in (2, 3, 4, 5)]
    assert conf == sorted(conf), f"уверенность должна расти с числом групп: {conf}"
    assert conf[-1] > conf[0]


def test_confidence_bounded_zero_to_ten():
    fs = FactorSet(factors=[
        f("t", Group.TREND, 1.0), f("s", Group.STRUCTURE, 1.0),
        f("m", Group.MOMENTUM, 1.0), f("smc", Group.SMC, 1.0),
        f("v", Group.VOLUME, 1.0), f("l", Group.LEVELS, 1.0),
        f("d", Group.DERIVATIVES, 1.0), f("c", Group.CONTEXT, 1.0),
    ])
    res = score_factors(fs)
    assert 0.0 <= res.confidence <= 10.0


# ---------------------------------------------------------------------------
# Потенциал и качество
# ---------------------------------------------------------------------------

def test_potential_raises_confidence_but_not_direction():
    base = [f("t", Group.TREND, 0.8), f("s", Group.STRUCTURE, 0.7)]
    without = score_factors(FactorSet(factors=base))
    with_potential = score_factors(FactorSet(factors=base + [
        f("p", Group.POTENTIAL, 1.0), f("p2", Group.POTENTIAL, 0.9)]))
    assert with_potential.confidence > without.confidence
    assert with_potential.direction == without.direction
    assert with_potential.potential > 0.8


def test_low_potential_reduces_confidence():
    base = [f("t", Group.TREND, 0.8), f("s", Group.STRUCTURE, 0.7)]
    high = score_factors(FactorSet(factors=base + [f("p", Group.POTENTIAL, 1.0)]))
    low = score_factors(FactorSet(factors=base + [f("p", Group.POTENTIAL, 0.05)]))
    assert low.confidence < high.confidence


def test_quality_penalty_reduces_confidence():
    base = [f("t", Group.TREND, 0.8), f("s", Group.STRUCTURE, 0.7)]
    clean = score_factors(FactorSet(factors=base, data_confidence=1.0))
    dirty = score_factors(FactorSet(factors=base + [
        f("q", Group.QUALITY, -0.8, evidence="нет истории")], data_confidence=0.4))
    assert dirty.confidence < clean.confidence
    assert dirty.quality < clean.quality


def test_quality_factors_do_not_change_direction():
    base = [f("t", Group.TREND, 0.8), f("s", Group.STRUCTURE, 0.7)]
    clean = score_factors(FactorSet(factors=base))
    dirty = score_factors(FactorSet(factors=base + [f("q", Group.QUALITY, -1.0)]))
    assert clean.direction == dirty.direction


# ---------------------------------------------------------------------------
# Объяснения
# ---------------------------------------------------------------------------

def test_evidence_split_into_bullish_and_bearish():
    fs = FactorSet(factors=[
        f("t", Group.TREND, 0.8, evidence="тренд вверх"),
        f("s", Group.STRUCTURE, 0.7, evidence="пробой структуры вверх"),
        f("m", Group.MOMENTUM, -0.7, evidence="RSI перекуплен"),
    ])
    res = score_factors(fs)
    assert res.direction is Direction.LONG
    assert "тренд вверх" in res.bullish
    assert "RSI перекуплен" in res.bearish
    assert res.top_evidence(1) == ["тренд вверх"]
    assert res.risks(1) == ["RSI перекуплен"]


def test_labels_are_readable():
    assert confidence_label(9.0) == "очень высокая"
    assert confidence_label(7.5) == "высокая"
    assert confidence_label(6.0) == "средняя"
    assert confidence_label(4.5) == "низкая"
    assert confidence_label(2.0) == "очень низкая"
    assert "высокий потенциал" in potential_label(0.9)
    assert "спит" in potential_label(0.1)


def test_group_score_helper():
    fs = FactorSet(factors=[f("t", Group.TREND, 0.5)])
    res = score_factors(fs)
    assert res.group_score(Group.TREND) == pytest.approx(0.5)
    assert res.group_score("нет_такой_группы") == 0.0
