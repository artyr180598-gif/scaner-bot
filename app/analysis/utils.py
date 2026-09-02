"""
app/analysis/utils.py — вспомогательная математика для модулей анализа.

Здесь же — поиск дивергенций (классических и скрытых) по подтверждённым свингам.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from app.indicators.structure import KIND_LOW, Swing


def clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    return float(max(lo, min(hi, value)))


def scaled(value: float, lo: float, hi: float) -> float:
    """
    Линейно растягивает ``value`` из [lo, hi] в [-1, 1].

    Пример: ``scaled(adx, 15, 40)`` → 0 при ADX 15 (нет тренда),
    +1 при ADX ≥ 40 (сильный тренд).
    """
    if value is None or math.isnan(value) or hi == lo:
        return 0.0
    raw = (value - lo) / (hi - lo) * 2 - 1
    return clamp(raw)


def band(value: float, center: float, half_width: float) -> float:
    """1 в центре полосы, 0 на краях — для «цена у уровня»."""
    if math.isnan(value) or half_width <= 0:
        return 0.0
    return clamp(1 - abs(value - center) / half_width, 0.0, 1.0)


def is_nan(value: Optional[float]) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def finite(value: Optional[float], default: float = float("nan")) -> float:
    return default if is_nan(value) else float(value)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Дивергенции
# ---------------------------------------------------------------------------

def _values_at(series: pd.Series, indexes: List[int]) -> List[float]:
    out = []
    for i in indexes:
        if 0 <= i < len(series):
            val = series.iloc[i]
            if val is not None and not pd.isna(val):
                out.append(float(val))
    return out


def detect_divergence(
    close: pd.Series,
    oscillator: pd.Series,
    swings: List[Swing],
    kind: str = "classic",
    max_bars_between: int = 90,
) -> Tuple[Optional[str], str]:
    """
    Дивергенция цены и осциллятора.

    ``classic``:
      * бычья — цена делает более низкий лоу, осциллятор — более высокий;
      * медвежья — цена делает более высокий хай, осциллятор — более низкий.
    ``hidden`` (продолжение тренда):
      * бычья — цена выше предыдущего лоу, осциллятор ниже;
      * медвежья — цена ниже предыдущего хая, осциллятор выше.

    Возвращает (тип: 'bullish'|'bearish'|None, текстовое объяснение).
    """
    if len(swings) < 2 or oscillator is None or len(oscillator) == 0:
        return None, ""
    n = len(close)

    for want_low in (True, False):
        kind_key = KIND_LOW if want_low else "high"
        pts = [s for s in swings if (s.kind == KIND_LOW) == want_low][-4:]
        if len(pts) < 2:
            continue
        for a, b in zip(pts[:-1], pts[1:]):
            if b.index - a.index > max_bars_between:
                continue
            if n - 1 - b.index > 45:      # слишком старая дивергенция
                continue
            osc_vals = _values_at(oscillator, [a.index, b.index])
            if len(osc_vals) != 2:
                continue
            osc_a, osc_b = osc_vals
            price_up = b.price > a.price
            osc_up = osc_b > osc_a

            if kind == "classic":
                if want_low and not price_up and osc_up and osc_b < 45:
                    return "bullish", (
                        f"бычья дивергенция: цена обновила минимум "
                        f"({a.price:.6g} → {b.price:.6g}), индикатор — нет "
                        f"({osc_a:.1f} → {osc_b:.1f})")
                if not want_low and price_up and not osc_up and osc_b > 55:
                    return "bearish", (
                        f"медвежья дивергенция: цена обновила максимум "
                        f"({a.price:.6g} → {b.price:.6g}), индикатор слабее "
                        f"({osc_a:.1f} → {osc_b:.1f})")
            else:  # hidden
                if want_low and price_up and not osc_up:
                    return "bullish", (
                        f"скрытая бычья дивергенция: лоу выше прежнего, "
                        f"импульс reset ({osc_a:.1f} → {osc_b:.1f}) — признак "
                        f"продолжения роста")
                if not want_low and not price_up and osc_up:
                    return "bearish", (
                        f"скрытая медвежья дивергенция: хай ниже прежнего при "
                        f"восстановлении индикатора ({osc_a:.1f} → {osc_b:.1f})")
    return None, ""


def rejection_wick(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    bars: int = 3,
) -> Tuple[float, str]:
    """
    Оценка «отбоя» последних ``bars`` баров в диапазоне [-1, 1].

    Считаем долю тени относительно тела: длинная нижняя тень у поддержки —
    это откуп (бычий признак), длинная верхняя — отпродажа.
    """
    n = len(close)
    if n < bars:
        return 0.0, ""
    o = open_.iloc[-bars:].to_numpy(dtype="float64")
    h = high.iloc[-bars:].to_numpy(dtype="float64")
    lo = low.iloc[-bars:].to_numpy(dtype="float64")
    c = close.iloc[-bars:].to_numpy(dtype="float64")
    rng = np.maximum(h - lo, 1e-12)
    body_top = np.maximum(o, c)
    body_bot = np.minimum(o, c)
    upper_wick = (h - body_top) / rng
    lower_wick = (body_bot - lo) / rng
    up = float(np.nanmean(upper_wick))
    dn = float(np.nanmean(lower_wick))
    score = clamp(dn - up)
    if score > 0.25:
        return score, f"нижние тени {dn * 100:.0f}% диапазона — откуп снизу"
    if score < -0.25:
        return score, f"верхние тени {up * 100:.0f}% диапазона — отпродажа сверху"
    return score, ""


def pct(a: float, b: float) -> float:
    """Процентное изменение от b к a (без деления на ноль)."""
    if is_nan(a) or is_nan(b) or b == 0:
        return float("nan")
    return (a / b - 1) * 100
