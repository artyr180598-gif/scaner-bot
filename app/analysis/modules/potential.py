"""
app/analysis/modules/potential.py — ненаправленные факторы ПОТЕНЦИАЛА движения.

Это главная идея продукта: искать монеты, у которых есть ЗАДАТКИ сильного
движения, а не те, что уже улетели. Такие факторы не голосуют «за лонг/шорт» —
они повышают вероятность того, что движение вообще будет (и что сигнал не
протухнет в боковике). В скоринге они влияют на потенциал и уверенность,
но не на направление.

Все факторы группы POTENTIAL имеют score в диапазоне [0, 1].
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp, finite, scaled
from app.domain.models import Factor


@analysis_module("volatility_compression", Group.POTENTIAL,
                 description="Сжатие волатильности — сжатая пружина")
def volatility_compression(features: MarketFeatures) -> Iterable[Factor]:
    states = list(features.volatility.values())
    if not states:
        return []
    compression = max(s.compression for s in states)
    if compression < 0.25:
        return []
    details = []
    for tf, st in features.volatility.items():
        if st.compression >= 0.45:
            details.append(
                f"{tf.value}: ATR на {st.atr_percentile:.0f} перцентиле, "
                f"BB {st.bb_width_percentile:.0f}")
    note = ("волатильность сжата" + (f" ({'; '.join(details[:2])})" if details else ""))
    if any(s.squeeze_active for s in states):
        note += ", сжатие BB внутри KC (squeeze)"
    if any(s.is_nr7 for s in states):
        note += ", NR7"
    yield Factor("volatility_compression", Group.POTENTIAL,
                 clamp(compression * 1.15), note, weight=1.4)


@analysis_module("energy_buildup", Group.POTENTIAL,
                 description="Набор позиции: объём/OI растут, цена стоит")
def energy_buildup(features: MarketFeatures) -> Iterable[Factor]:
    m = features.metrics
    vol_z = finite(m.get("volume_z", float("nan")), 0.0)
    oi_change = finite(m.get("oi_change_pct", float("nan")), float("nan"))
    change_24h = finite(m.get("change_24h", float("nan")), 0.0)
    change_7d = finite(m.get("change_7d", float("nan")), 0.0)

    parts: list[float] = []
    notes: list[str] = []
    # Объёмный всплеск без пропорционального движения цены.
    if vol_z >= 1.5 and abs(change_24h) < 5:
        parts.append(clamp(scaled(vol_z, 1.5, 4.0)))
        notes.append(f"объём z={vol_z:.1f} при движении {change_24h:+.1f}% за 24ч")
    if oi_change == oi_change and oi_change > 5 and abs(change_7d) < 8:
        parts.append(clamp(scaled(oi_change, 5, 25)))
        notes.append(f"OI +{oi_change:.0f}% за 24ч при цене {change_7d:+.1f}% за 7д")
    obv_slope = finite(m.get("obv_slope_pct", float("nan")), 0.0)
    if abs(obv_slope) > 8 and abs(change_7d) < 8:
        parts.append(0.5)
        notes.append(f"OBV {'+' if obv_slope > 0 else ''}{obv_slope:.0f}% без движения цены")

    if not parts:
        return []
    yield Factor("energy_buildup", Group.POTENTIAL, clamp(sum(parts) / len(parts) * 1.1),
                 "; ".join(notes), weight=1.1)


@analysis_module("level_proximity", Group.POTENTIAL,
                 description="Близость к значимому уровню — триггер движения")
def level_proximity(features: MarketFeatures) -> Iterable[Factor]:
    price = features.price
    atr = features.atr
    if not atr or atr != atr:
        return []
    best = None
    for lv in features.levels:
        dist = abs(lv.price - price) / atr
        if dist > 2.0:
            continue
        weight = lv.strength * (1 - dist / 2.0)
        if best is None or weight > best[0]:
            best = (weight, lv, dist)
    if best is None or best[0] < 0.2:
        return []
    _, lv, dist = best
    yield Factor(
        "level_proximity", Group.POTENTIAL, clamp(best[0] * 1.2),
        f"до значимого уровня {lv.price:.6g} ({lv.source}) всего {dist:.1f} ATR — "
        f"пробой или отбой даст движение",
        weight=1.0,
    )


@analysis_module("liquidity_target_space", Group.POTENTIAL,
                 description="Есть куда двигаться: свободное пространство до ликвидности")
def liquidity_target_space(features: MarketFeatures) -> Iterable[Factor]:
    """
    Если ближайший пул ликвидности/уровень далеко — движение будет долгим.
    Если цена зажата между уровнями в 0.5 ATR — потенциал низкий.
    """
    atr = features.atr
    price = features.price
    if not atr or atr != atr or not features.levels:
        return []
    ups = [lv.price for lv in features.levels if lv.price > price]
    downs = [lv.price for lv in features.levels if lv.price < price]
    space_up = (min(ups) - price) / atr if ups else 6.0
    space_dn = (price - max(downs)) / atr if downs else 6.0
    room = max(space_up, space_dn)
    tight = min(space_up, space_dn)
    if tight < 0.6 and room < 2.0:
        yield Factor("liquidity_target_space", Group.POTENTIAL, 0.05,
                     f"цена зажата между уровнями ({tight:.1f} ATR) — двигаться некуда",
                     weight=0.8)
        return
    if room >= 2.0:
        side = "вверх" if space_up >= space_dn else "вниз"
        yield Factor("liquidity_target_space", Group.POTENTIAL,
                     clamp(scaled(room, 1.5, 6.0) * 0.5 + 0.5),
                     f"свободное пространство {room:.1f} ATR {side} до ближайшей цели",
                     weight=0.7)


@analysis_module("not_extended", Group.POTENTIAL,
                 description="Монета ещё не сделала своё движение (анти-погоня)")
def not_extended(features: MarketFeatures) -> Iterable[Factor]:
    """
    Ключевой фильтр продукта: нам нужны монеты ДО движения.
    Уже улетевшая монета получает низкий потенциал (шанс продолжения есть,
    но риск/вознаграждение хуже, и это уже не «перспектива», а погоня).
    """
    m = features.metrics
    change_7d = finite(m.get("change_7d", float("nan")), 0.0)
    change_30d = finite(m.get("change_30d", float("nan")), 0.0)
    stretch = finite(m.get("stretch_atr", float("nan")), 0.0)
    from_high = finite(m.get("distance_from_high_pct", float("nan")), 0.0)

    score = 0.8
    notes: list[str] = []
    if abs(change_7d) > 25:
        score -= 0.5
        notes.append(f"уже {change_7d:+.0f}% за 7 дней")
    elif abs(change_7d) > 12:
        score -= 0.2
        notes.append(f"уже {change_7d:+.0f}% за 7 дней")
    else:
        notes.append(f"за 7 дней {change_7d:+.1f}% — движение ещё не сделано")
    if abs(stretch) > 2.5:
        score -= 0.25
        notes.append(f"растянутость {stretch:+.1f} ATR от EMA20")
    if change_30d == change_30d and abs(change_30d) > 60:
        score -= 0.15
        notes.append(f"{change_30d:+.0f}% за 30 дней")
    if from_high == from_high and -8 < from_high < -1:
        score += 0.1
        notes.append(f"{abs(from_high):.0f}% от максимума — есть запас")
    yield Factor("not_extended", Group.POTENTIAL, clamp(score, 0.0, 1.0),
                 "; ".join(notes), weight=1.2)
