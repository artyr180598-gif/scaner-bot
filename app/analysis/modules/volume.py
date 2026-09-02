"""
app/analysis/modules/volume.py — объём как подтверждение и как след.

Что важно для поиска «ещё не улетевших» монет:
  * аномальный объём БЕЗ большого движения цены = кто-то набирает позицию;
  * рост OBV при плоской цене = накопление;
  * усыхание объёма в сжатии = продавцы закончились (потенциал, не направление).
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp, finite, scaled
from app.domain.models import Factor


@analysis_module("volume_confirmation", Group.VOLUME,
                 description="Аномальный объём как подтверждение движения")
def volume_confirmation(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    z = finite(ind.volume_z, float("nan"))
    if z != z or z < 1.0:
        return []
    candles = ind.candles
    if len(candles) < 2:
        return []
    # Смотрим на ПОСЛЕДНИЙ ЗАКРЫТЫЙ бар: незакрытый бар объёмом не оценивают.
    idx = -2 if not candles.last_closed else -1
    idx = max(-len(candles), idx)
    o = float(candles.open.iloc[idx])
    c = float(candles.close.iloc[idx])
    h = float(candles.high.iloc[idx])
    lo = float(candles.low.iloc[idx])
    body = abs(c - o)
    rng = max(h - lo, 1e-12)
    direction = 1.0 if c > o else (-1.0 if c < o else 0.0)
    quality = clamp(body / rng)          # тело больше тени = намеренное движение
    magnitude = scaled(z, 1.0, 3.5)
    score = clamp(direction * magnitude * (0.5 + 0.5 * quality))
    side = "покупки" if direction > 0 else ("продажи" if direction < 0 else "борьба")
    yield Factor(
        "volume_confirmation", Group.VOLUME, score,
        f"объём в {finite(ind.volume_ratio, 1.0):.1f}× выше среднего "
        f"(z={z:.1f}) — преобладали {side}",
        weight=1.1,
    )


@analysis_module("accumulation_flow", Group.VOLUME,
                 description="Накопление: OBV и денежный поток (MFI)")
def accumulation_flow(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    obv_slope = finite(ind.obv_slope_pct, float("nan"))
    mfi = finite(ind.mfi, float("nan"))
    change = finite(features.metrics.get("change_7d", float("nan")), 0.0)
    if obv_slope != obv_slope:
        return []

    obv_vote = clamp(obv_slope / 12.0)
    mfi_vote = scaled(mfi, 35, 65) if mfi == mfi else 0.0
    score = clamp(0.65 * obv_vote + 0.35 * mfi_vote)

    # Классическое накопление: объёмы растут, а цена почти не двигалась.
    note = f"OBV {'растёт' if obv_slope > 0 else 'падает'} на {abs(obv_slope):.1f}% за 20 баров"
    if mfi == mfi:
        note += f", MFI {mfi:.0f}"
    if obv_slope > 5 and abs(change) < 6:
        note += " — накопление без движения цены"
        score = clamp(score + 0.2 * (1 if obv_slope > 0 else -1))
    yield Factor("accumulation_flow", Group.VOLUME, score, note, weight=1.0)


@analysis_module("effort_vs_result", Group.VOLUME,
                 description="Усилие против результата: объём есть — цены нет")
def effort_vs_result(features: MarketFeatures) -> Iterable[Factor]:
    """
    Принцип Вайкоффа: большой объём при маленьком теле свечи — это поглощение
    (крупный игрок забирает ликвидность). Направление — против движения бара.
    """
    ind = features.primary()
    candles = ind.candles
    if len(candles) < 4:
        return []
    ratio = finite(ind.volume_ratio, float("nan"))
    if ratio != ratio or ratio < 1.3:
        return []
    body = abs(float(candles.close.iloc[-1]) - float(candles.open.iloc[-1]))
    rng = max(float(candles.high.iloc[-1]) - float(candles.low.iloc[-1]), 1e-12)
    body_ratio = body / rng
    if body_ratio > 0.55:
        return []          # нормальное импульсное движение, не поглощение
    up_bar = float(candles.close.iloc[-1]) > float(candles.open.iloc[-1])
    score = clamp(0.55 * scaled(ratio, 1.3, 3.0) * (-1.0 if up_bar else 1.0))
    yield Factor(
        "effort_vs_result", Group.VOLUME, score,
        f"объём {ratio:.1f}× при теле свечи {body_ratio * 100:.0f}% диапазона — "
        f"{'поглощение продаж' if not up_bar else 'поглощение покупок'}",
        weight=0.7,
    )
