"""
app/analysis/modules/structure.py — структура рынка: BOS/CHOCH, HH/HL, свипы.
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp
from app.domain.models import Factor
from app.indicators.structure import DIR_UP, EVENT_BOS, EVENT_CHOCH


def _recency_decay(bars_ago: int, half_life: int = 12) -> float:
    """Свежее событие весит больше: 1.0 сейчас, 0.5 через half_life баров."""
    if bars_ago <= 0:
        return 1.0
    return float(0.5 ** (bars_ago / half_life))


@analysis_module("structure_event", Group.STRUCTURE,
                 description="Последний пробой структуры (BOS) или смена характера (CHOCH)")
def structure_event(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    struct = features.struct(ind.timeframe)
    if struct is None or struct.last_event is None:
        return []
    ev = struct.last_event
    if ev.bars_ago > 40:
        return []
    base = 0.9 if ev.kind == EVENT_BOS else 0.75
    direction = 1.0 if ev.direction == DIR_UP else -1.0
    score = clamp(direction * base * (0.45 + 0.55 * _recency_decay(ev.bars_ago)))
    label = "пробой структуры (BOS)" if ev.kind == EVENT_BOS else \
        "смена характера рынка (CHOCH)"
    yield Factor(
        "structure_event", Group.STRUCTURE, score,
        f"{label} {'вверх' if direction > 0 else 'вниз'} {ev.bars_ago} бар(ов) назад "
        f"на уровне {ev.level:.6g}",
        weight=1.3,
    )


@analysis_module("swing_pattern", Group.STRUCTURE,
                 description="Паттерн свингов: HH/HL против LH/LL")
def swing_pattern(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    struct = features.struct(ind.timeframe)
    if struct is None:
        return []
    if struct.hh_hl:
        score, note = 0.7, "структура бычья: максимумы и минимумы повышаются (HH/HL)"
    elif struct.lh_ll:
        score, note = -0.7, "структура медвежья: максимумы и минимумы понижаются (LH/LL)"
    else:
        return []
    yield Factor("swing_pattern", Group.STRUCTURE, score, note, weight=1.0)


@analysis_module("liquidity_sweep", Group.STRUCTURE,
                 description="Снятие ликвидности (stop hunt) с возвратом в диапазон")
def liquidity_sweep(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    struct = features.struct(ind.timeframe)
    if struct is None or not struct.sweeps:
        return []
    sweep = struct.sweeps[0]
    if sweep.bars_ago > 12:
        return []
    # Свип снизу → бычий разворот, свип сверху → медвежий.
    direction = -1.0 if sweep.direction == DIR_UP else 1.0
    score = clamp(direction * (0.6 + 0.3 * _recency_decay(sweep.bars_ago, 6)))
    where = "сверху (сняли стопы покупателей)" if sweep.direction == DIR_UP \
        else "снизу (выбили стопы продавцов)"
    yield Factor(
        "liquidity_sweep", Group.STRUCTURE, score,
        f"снятие ликвидности {where} {sweep.bars_ago} бар(ов) назад, "
        f"цена вернулась в диапазон — признак разворота",
        weight=1.0,
    )


@analysis_module("mt_structure", Group.STRUCTURE,
                 description="Согласованность структуры на старших таймфреймах")
def mt_structure(features: MarketFeatures) -> Iterable[Factor]:
    higher = features.higher_tfs()
    scores: list[float] = []
    notes: list[str] = []
    for tf in higher:
        struct = features.struct(tf)
        if struct is None:
            continue
        if struct.trend == "up":
            scores.append(1.0)
            notes.append(f"{tf.value} ↑")
        elif struct.trend == "down":
            scores.append(-1.0)
            notes.append(f"{tf.value} ↓")
        else:
            notes.append(f"{tf.value} —")
    if not scores:
        return []
    score = clamp(sum(scores) / len(scores) * min(1.0, len(scores) / 2))
    yield Factor("mt_structure", Group.STRUCTURE, score,
                 f"структура старших ТФ: {', '.join(notes)}", weight=0.9)
