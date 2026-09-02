"""
app/analysis/modules/trend.py — тренд и мульти-таймфреймное выравнивание.

Идея из freqtrade (informative pairs): старшие таймфреймы задают контекст,
младший — точку входа. Все факторы здесь в ОДНОЙ группе `trend`, потому что
«тренд на 4h» и «тренд на 1d» — это одно наблюдение, а не два голоса.
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.features import TF_WEIGHT, timeframe_trend_score
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp, finite, scaled
from app.domain.models import Factor


@analysis_module("mt_trend_alignment", Group.TREND,
                 description="Согласованность тренда на старших таймфреймах")
def mt_trend_alignment(features: MarketFeatures) -> Iterable[Factor]:
    higher = features.higher_tfs()
    if not higher:
        return []
    num = 0.0
    den = 0.0
    parts: list[str] = []
    for tf in higher:
        ind = features.ind(tf)
        if ind is None or not ind.usable:
            continue
        score, note = timeframe_trend_score(ind)
        weight = TF_WEIGHT.get(tf, 0.7)
        num += score * weight
        den += weight
        if note:
            parts.append(f"{tf.value}: {note}")
    if den == 0:
        return []
    score = clamp(num / den)
    if abs(score) < 0.12:
        text = "старшие ТФ в боковике, единого тренда нет"
    else:
        text = " | ".join(parts) or "тренд старших ТФ"
    yield Factor("mt_trend_alignment", Group.TREND, score, text, weight=1.3)


@analysis_module("trend_strength", Group.TREND,
                 description="Сила тренда по ADX с направлением по DI")
def trend_strength(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    adx = finite(ind.adx, float("nan"))
    pdi, mdi = finite(ind.plus_di, float("nan")), finite(ind.minus_di, float("nan"))
    if adx != adx:
        return []
    if pdi == pdi and mdi == mdi:
        direction = 1.0 if pdi > mdi else (-1.0 if mdi > pdi else 0.0)
        di_edge = scaled(abs(pdi - mdi), 3, 18)
    else:
        direction = 1.0 if ind.price > finite(ind.ema50, ind.price) else -1.0
        di_edge = 0.5
    strength = max(scaled(adx, 16, 38), 0.0)
    score = clamp(direction * strength * (0.6 + 0.4 * di_edge))
    if adx < 18:
        text = f"ADX {adx:.0f} — тренда нет (флэт)"
        score = 0.0
    else:
        side = "восходящий" if direction > 0 else "нисходящий"
        text = f"ADX {adx:.0f} — {side} тренд"
    yield Factor("trend_strength", Group.TREND, score, text, weight=1.0)


@analysis_module("ema_regime", Group.TREND,
                 description="Положение цены относительно ключевых EMA рабочего ТФ")
def ema_regime(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    price = ind.price
    votes: list[float] = []
    labels: list[str] = []
    for name, value in (("EMA20", ind.ema20), ("EMA50", ind.ema50), ("EMA100", ind.ema100)):
        if finite(value) and value > 0:
            votes.append(clamp((price / value - 1) / 0.03))
            labels.append(f"{name} {'выше' if price > value else 'ниже'}")
    if not votes:
        return []
    score = clamp(sum(votes) / len(votes))
    yield Factor("ema_regime", Group.TREND, score,
                 f"цена {', '.join(labels[:3])}", weight=0.7)
