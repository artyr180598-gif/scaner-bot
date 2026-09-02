"""
app/analysis/modules/smc.py — смарт-мани концепции: Order Blocks, FVG, ликвидность.

Логика: цена возвращается в зоны, где остался незакрытый интерес крупного
игрока (OB/FVG), и тянется к пулам ликвидности (равные хаи/лои, свинги).
Поэтому эти зоны работают и как точки входа, и как цели.
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp, scaled
from app.domain.models import Factor


@analysis_module("order_block", Group.SMC,
                 description="Цена у незакрытого Order Block — зона интереса")
def order_block(features: MarketFeatures) -> Iterable[Factor]:
    price = features.price
    atr = features.atr
    if not atr or atr != atr:
        return []
    best = None
    best_dist = float("inf")
    for struct in features.structure.values():
        for zone in struct.order_blocks:
            if zone.mitigated:
                continue
            dist = abs(zone.distance_pct(price)) * price / 100
            if dist < best_dist and dist <= 2.5 * atr:
                best, best_dist = zone, dist
    if best is None:
        return []
    direction = 1.0 if best.is_bullish else -1.0
    proximity = 1.0 - min(1.0, best_dist / (2.5 * atr))
    score = clamp(direction * (0.5 + 0.45 * proximity) * (0.6 + 0.4 * best.strength))
    kind = "бычий" if best.is_bullish else "медвежий"
    inside = "цена внутри зоны" if best_dist == 0 else \
        f"{best_dist / atr:.1f} ATR до зоны"
    yield Factor(
        "order_block", Group.SMC, score,
        f"{kind} Order Block {best.low:.6g}–{best.high:.6g} не закрыт, {inside}; "
        f"тестов: {best.touches}",
        weight=1.2,
    )


@analysis_module("fvg_magnet", Group.SMC,
                 description="Незаполненные имбалансы (FVG) как магнит цены")
def fvg_magnet(features: MarketFeatures) -> Iterable[Factor]:
    price = features.price
    atr = features.atr
    if not atr or atr != atr:
        return []
    above = None
    below = None
    for struct in features.structure.values():
        for zone in struct.fvgs:
            if zone.mitigated:
                continue
            dist = abs(zone.distance_pct(price)) * price / 100
            if dist > 6 * atr:
                continue
            if zone.mid > price and (above is None or dist < abs(above[1])):
                above = (zone, dist)
            elif zone.mid < price and (below is None or dist < abs(below[1])):
                below = (zone, dist)
    if above is None and below is None:
        return []
    # Ближайший незаполненный имбаланс — цель движения.
    if above is not None and (below is None or above[1] <= below[1]):
        zone, dist = above
        score = clamp(0.55 * (1 - min(1.0, dist / (6 * atr))))
        text = (f"незаполненный бычий имбаланс выше ({zone.low:.6g}–{zone.high:.6g}), "
                f"{dist / atr:.1f} ATR — магнит для цены")
    else:
        zone, dist = below  # type: ignore[misc]
        score = clamp(-0.55 * (1 - min(1.0, dist / (6 * atr))))
        text = (f"незаполненный медвежий имбаланс ниже ({zone.low:.6g}–{zone.high:.6g}), "
                f"{dist / atr:.1f} ATR — цена стремится его закрыть")
    yield Factor("fvg_magnet", Group.SMC, score, text, weight=0.8)


@analysis_module("liquidity_draw", Group.SMC,
                 description="Пулы ликвидности: равные хаи/лои как цель движения")
def liquidity_draw(features: MarketFeatures) -> Iterable[Factor]:
    price = features.price
    atr = features.atr
    if not atr or atr != atr:
        return []
    pools_above: list[float] = []
    pools_below: list[float] = []
    for struct in features.structure.values():
        pools_above += [lv for lv in struct.equal_highs if lv > price]
        pools_below += [lv for lv in struct.equal_lows if lv < price]
    if not pools_above and not pools_below:
        return []
    d_above = min((abs(lv - price) for lv in pools_above), default=float("inf"))
    d_below = min((abs(lv - price) for lv in pools_below), default=float("inf"))
    if d_above <= d_below:
        score = clamp(0.5 * scaled(10 * atr, d_above, 10 * atr) if d_above < float("inf") else 0)
        text = (f"пул ликвидности выше на {d_above / atr:.1f} ATR "
                f"(равные максимумы) — цель для движения вверх")
    else:
        score = clamp(-0.5 * scaled(10 * atr, d_below, 10 * atr) if d_below < float("inf") else 0)
        text = (f"пул ликвидности ниже на {d_below / atr:.1f} ATR "
                f"(равные минимумы) — цель для движения вниз")
    if score == 0:
        return []
    yield Factor("liquidity_draw", Group.SMC, score, text, weight=0.7)
