"""
app/analysis/modules/levels.py — уровни: реакция у поддержки/сопротивления,
позиция в диапазоне, профиль объёма.

Все три фактора по природе КОНТРТРЕНДОВЫЕ (возврат к уровню/середине), поэтому
они гасятся, когда старшие таймфреймы сильно трендят против идеи: во флэте
уровни работают, в тренде — превращаются в ловлю падающих ножей.
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.features import counter_trend_damping
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp, rejection_wick, scaled
from app.domain.models import Factor


@analysis_module("level_reaction", Group.LEVELS,
                 description="Реакция цены у значимого уровня (свеча-отбой)")
def level_reaction(features: MarketFeatures) -> Iterable[Factor]:
    price = features.price
    atr = features.atr
    if not atr or atr != atr:
        return []
    support = features.nearest_level("support", max_distance_atr=1.5)
    resistance = features.nearest_level("resistance", max_distance_atr=1.5)
    if support is None and resistance is None:
        return []

    ind = features.primary()
    wick_score, wick_note = rejection_wick(
        ind.candles.open, ind.candles.high, ind.candles.low, ind.candles.close, bars=3)

    candidates: list[tuple[float, str, float]] = []
    if support is not None:
        dist = abs(support.price - price) / atr
        strength = 0.4 + 0.5 * support.strength + 0.1 * min(support.touches, 3)
        score = clamp(strength * (1 - min(1.0, dist / 1.5)) * (0.7 + 0.3 * max(wick_score, 0)))
        candidates.append((score, (
            f"цена у поддержки {support.price:.6g} ({support.source}, "
            f"{dist:.1f} ATR)" + (f", {wick_note}" if wick_score > 0.25 else "")), 1.0))
    if resistance is not None:
        dist = abs(resistance.price - price) / atr
        strength = 0.4 + 0.5 * resistance.strength + 0.1 * min(resistance.touches, 3)
        score = clamp(-strength * (1 - min(1.0, dist / 1.5)) *
                      (0.7 + 0.3 * max(-wick_score, 0)))
        candidates.append((score, (
            f"цена у сопротивления {resistance.price:.6g} ({resistance.source}, "
            f"{dist:.1f} ATR)" + (f", {wick_note}" if wick_score < -0.25 else "")), -1.0))

    score, text, sign = max(candidates, key=lambda c: abs(c[0]))
    damping = counter_trend_damping(features, sign)
    score *= damping
    if abs(score) < 0.1:
        return []
    if damping < 0.9:
        text += f" (ослаблено: старшие ТФ против, ×{damping:.2f})"
    yield Factor("level_reaction", Group.LEVELS, score, text, weight=1.1)


@analysis_module("range_position", Group.LEVELS,
                 description="Положение в 30-дневном диапазоне (возврат к середине)")
def range_position(features: MarketFeatures) -> Iterable[Factor]:
    pos = features.metrics.get("range_position", float("nan"))
    compression = features.metrics.get("compression", float("nan"))
    if pos != pos:
        return []
    # Возврат к середине работает только во флэте/сжатии — иначе это контртренд.
    regime_weight = 0.0
    if compression == compression and compression >= 0.5:
        regime_weight = 0.55
    elif features.metrics.get("choppiness", 0) > 61.8:
        regime_weight = 0.4
    if regime_weight == 0:
        return []
    score = clamp((0.5 - pos) * 2 * regime_weight)
    sign = 1.0 if score > 0 else -1.0
    damping = counter_trend_damping(features, sign, max_damping=0.75)
    score *= damping
    if abs(score) < 0.15:
        return []
    where = "нижней" if pos < 0.5 else "верхней"
    yield Factor(
        "range_position", Group.LEVELS, score,
        f"цена у {where} границы диапазона ({pos * 100:.0f}%) при сжатии "
        f"волатильности — возможен возврат к середине"
        + ("" if damping > 0.9 else f" (ослаблено трендом, ×{damping:.2f})"),
        weight=0.6,
    )


@analysis_module("volume_profile_position", Group.LEVELS,
                 description="Позиция относительно POC/Value Area профиля объёма")
def volume_profile_position(features: MarketFeatures) -> Iterable[Factor]:
    vp = features.profile
    atr = features.atr
    price = features.price
    if vp is None or not atr or atr != atr:
        return []
    d_poc = abs(price - vp.poc) / atr
    if price < vp.val:
        score = clamp(0.45 * scaled(6 * atr, abs(price - vp.val), 6 * atr))
        sign = 1.0
        text = (f"цена ниже зоны стоимости (VAL {vp.val:.6g}) — недооценена "
                f"относительно основного объёма, магнит: POC {vp.poc:.6g}")
    elif price > vp.vah:
        score = clamp(-0.45 * scaled(6 * atr, abs(price - vp.vah), 6 * atr))
        sign = -1.0
        text = (f"цена выше зоны стоимости (VAH {vp.vah:.6g}) — перегрев "
                f"относительно основного объёма")
    elif d_poc < 0.5:
        text = f"цена на POC ({vp.poc:.6g}) — зона равновесия"
        score = 0.0
        sign = 0.0
    else:
        return []
    if abs(score) < 0.08:
        return []
    damping = counter_trend_damping(features, sign)
    score *= damping
    yield Factor("volume_profile_position", Group.LEVELS, score, text, weight=0.6)
