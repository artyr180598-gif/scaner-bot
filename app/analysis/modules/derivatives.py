"""
app/analysis/modules/derivatives.py — фьючерсная информация: funding, OI, базис.

Funding и открытые позиции — это информация о ТОЛПЕ. Экстремальный funding
означает переполненную позицию, которая любит разгружаться через движение
против неё; рост OI без движения цены — набор позиции кем-то крупным.

Модуль полностью деградирует (не выдаёт факторов), если деривативных данных
нет — спотовая монета без перпетуала анализируется честнее, чем с нулями.
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp, finite, scaled
from app.domain.models import Factor

# Типичный «нормальный» funding — 0.01% за 8 часов.
FUNDING_NEUTRAL = 0.01
FUNDING_EXTREME = 0.06


@analysis_module("funding_crowding", Group.DERIVATIVES,
                 description="Funding rate: переполненная позиция толпы")
def funding_crowding(features: MarketFeatures) -> Iterable[Factor]:
    der = features.derivatives
    funding = der.funding_pct
    if funding is None or funding != funding:
        return []
    # Контртрендовая логика: дорогой лонг → риск выноса вниз и наоборот.
    if funding >= FUNDING_EXTREME:
        score = -clamp(scaled(funding, FUNDING_NEUTRAL, 0.15))
        note = (f"funding {funding:+.3f}%/8ч — лонги переполнены и платят, "
                f"риск выноса вниз")
    elif funding <= -FUNDING_NEUTRAL:
        score = clamp(scaled(-funding, 0.0, 0.10))
        note = (f"funding {funding:+.3f}%/8ч — шорты платят лонгам, "
                f"позиция толпы переполнена вниз")
    else:
        score = 0.0
        note = f"funding {funding:+.3f}%/8ч — нейтрально"
    if abs(score) < 0.08:
        return []
    yield Factor("funding_crowding", Group.DERIVATIVES, score, note, weight=0.9)


@analysis_module("open_interest_flow", Group.DERIVATIVES,
                 description="Open Interest против цены: набор или разгрузка позиции")
def open_interest_flow(features: MarketFeatures) -> Iterable[Factor]:
    der = features.derivatives
    oi_change = finite(der.open_interest_change_pct, float("nan"))
    if oi_change != oi_change:
        return []
    price_change = finite(features.metrics.get("change_24h", float("nan")), 0.0)
    # Квадранты OI/цена (классика фьючерсного анализа).
    if oi_change > 3 and price_change > 1:
        score, note = 0.7, (f"OI +{oi_change:.1f}% при цене {price_change:+.1f}% — "
                            f"новые лонги, тренд подтверждён")
    elif oi_change > 3 and price_change < -1:
        score, note = -0.7, (f"OI +{oi_change:.1f}% при цене {price_change:+.1f}% — "
                             f"новые шорты давят")
    elif oi_change > 3 and abs(price_change) <= 1:
        score, note = 0.45, (f"OI +{oi_change:.1f}% при плоской цене — "
                             f"крупный набор позиции перед движением")
    elif oi_change < -3:
        score, note = -0.15, (f"OI {oi_change:.1f}% — закрытие позиций, "
                              f"импульс выдыхается")
    else:
        return []
    yield Factor("open_interest_flow", Group.DERIVATIVES, clamp(score), note, weight=1.0)


@analysis_module("basis_premium", Group.DERIVATIVES,
                 description="Базис (марка к индексу): локальный перегрев")
def basis_premium(features: MarketFeatures) -> Iterable[Factor]:
    basis = features.derivatives.basis_pct
    if basis is None or basis != basis or abs(basis) < 0.08:
        return []
    score = clamp(-scaled(abs(basis), 0.08, 0.6) * (1 if basis > 0 else -1))
    side = "премия перпетуала к индексу" if basis > 0 else "дисконт перпетуала"
    yield Factor("basis_premium", Group.DERIVATIVES, score,
                 f"{side}: {basis:+.2f}%", weight=0.5)
