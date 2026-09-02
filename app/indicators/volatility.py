"""
app/indicators/volatility.py — измерение «сжатия пружины».

Ключевая идея сканера: искать монеты, где волатильность сжалась до
исторического минимума (низкий перцентиль ширины BB / ATR, NR7, узкий
Дончиан), но объём и открытые позиции уже начали расти. Такое состояние
предшествует импульсу — в отличие от уже улетевшей монеты, где движение
случилось.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from app.indicators.core import (atr, bollinger, donchian, keltner,
                                 natr, percentile_rank, true_range)


def realized_vol(close: pd.Series, period: int = 20) -> pd.Series:
    """Реализованная волатильность в % за бар (стандарт лог-доходностей)."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(period, min_periods=max(5, period // 2)).std(ddof=0) * 100


def choppiness_index(high: pd.Series, low: pd.Series, close: pd.Series,
                     period: int = 14) -> pd.Series:
    """
    Choppiness Index: >61.8 — рынок «пилит» (флэт/накопление),
    <38.2 — выраженный тренд.
    """
    period = int(period)
    tr_sum = true_range(high, low, close).rolling(period, min_periods=period).sum()
    hh = high.rolling(period, min_periods=period).max()
    ll = low.rolling(period, min_periods=period).min()
    rng = (hh - ll).replace(0, np.nan)
    return 100 * np.log10(tr_sum / rng) / np.log10(period)


def nr7(high: pd.Series, low: pd.Series) -> pd.Series:
    """True, если диапазон бара минимален за последние 7 баров."""
    rng = high - low
    return rng <= rng.rolling(7, min_periods=7).min()


@dataclass(slots=True)
class VolatilityState:
    """Снимок волатильности с интерпретацией для скоринга."""

    atr: float
    atr_pct: float
    atr_percentile: float          # 0..100, низкий = сжатие
    bb_width: float
    bb_width_percentile: float     # 0..100
    realized_vol: float
    choppiness: float
    is_nr7: bool
    squeeze_active: bool           # BB внутри KC
    compression: float             # 0..1 — интегральная «сжатость»
    range_position: float          # 0..1 — где цена внутри диапазона N баров
    donchian_high: float
    donchian_low: float

    def describe_ru(self) -> str:
        if self.compression >= 0.7:
            state = "сильное сжатие (пружина)"
        elif self.compression >= 0.45:
            state = "умеренное сжатие"
        elif self.compression <= 0.2:
            state = "расширение (импульс идёт)"
        else:
            state = "норма"
        return (f"волатильность: {state}; ATR {self.atr_pct:.2f}% "
                f"(перцентиль {self.atr_percentile:.0f}), "
                f"BB-ширина на {self.bb_width_percentile:.0f} перцентиле"
                + (", NR7" if self.is_nr7 else "")
                + (", сжатие BB<KC" if self.squeeze_active else ""))


def analyse_volatility(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_period: int = 14,
    bb_period: int = 20,
    percentile_window: int = 100,
    range_window: int = 30,
) -> Optional[VolatilityState]:
    """Считает всё сразу: один проход по данным, без дублирования окон."""
    if len(close) < max(percentile_window // 2, 30):
        return None

    a = atr(high, low, close, atr_period)
    a_pct = natr(high, low, close, atr_period)
    _, upper, lower, width = bollinger(close, bb_period, 2.0)
    _, kc_upper, kc_lower = keltner(high, low, close, bb_period, atr_period, 1.5)
    ch = choppiness_index(high, low, close, 14)

    atr_pct_rank = percentile_rank(a_pct.dropna(), percentile_window)
    width_rank = percentile_rank(width.dropna(), percentile_window)
    rvol = realized_vol(close, 20)
    dh, dl, _ = donchian(high, low, range_window)

    last_price = float(close.iloc[-1])
    hi, lo = float(dh.iloc[-1]), float(dl.iloc[-1])
    range_pos = (last_price - lo) / (hi - lo) if hi > lo else 0.5

    atr_rank = float(atr_pct_rank.iloc[-1]) if len(atr_pct_rank) and not pd.isna(atr_pct_rank.iloc[-1]) else 50.0
    w_rank = float(width_rank.iloc[-1]) if len(width_rank) and not pd.isna(width_rank.iloc[-1]) else 50.0
    squeeze = bool(upper.iloc[-1] < kc_upper.iloc[-1] and lower.iloc[-1] > kc_lower.iloc[-1]) \
        if not (pd.isna(upper.iloc[-1]) or pd.isna(kc_upper.iloc[-1])) else False

    # «Сжатость»: чем ниже перцентили волатильности — тем выше балл;
    # сжатие BB внутри KC и NR7 добавляют весомых бонусов.
    compression = (
        0.45 * (1 - atr_rank / 100)
        + 0.35 * (1 - w_rank / 100)
        + 0.10 * (0.5 if squeeze else 0.0) * 2
        + 0.10 * (0.5 if bool(nr7(high, low).iloc[-1]) else 0.0) * 2
    )
    compression = float(max(0.0, min(1.0, compression)))

    return VolatilityState(
        atr=float(a.iloc[-1]) if not pd.isna(a.iloc[-1]) else float("nan"),
        atr_pct=float(a_pct.iloc[-1]) if not pd.isna(a_pct.iloc[-1]) else float("nan"),
        atr_percentile=atr_rank,
        bb_width=float(width.iloc[-1]) if not pd.isna(width.iloc[-1]) else float("nan"),
        bb_width_percentile=w_rank,
        realized_vol=float(rvol.iloc[-1]) if not pd.isna(rvol.iloc[-1]) else float("nan"),
        choppiness=float(ch.iloc[-1]) if not pd.isna(ch.iloc[-1]) else 50.0,
        is_nr7=bool(nr7(high, low).iloc[-1]),
        squeeze_active=squeeze,
        compression=compression,
        range_position=float(max(0.0, min(1.0, range_pos))),
        donchian_high=hi,
        donchian_low=lo,
    )
