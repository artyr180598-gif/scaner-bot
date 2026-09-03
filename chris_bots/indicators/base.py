"""
Базовый класс индикатора.

Freqtrade-style: populate_indicators() — чистая функция (df) -> df.
Но мы оборачиваем в dataclass для регистрации + метаданных (group, weight).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass(slots=True)
class SignalSeries:
    """Серия сигналов одного индикатора: -1, 0, +1 (длинный/нейтрально/короткий)."""

    name: str
    series: pd.Series  # значения -1/0/+1
    raw: Optional[pd.Series] = None  # сырое значение индикатора (опц.)
    strength: Optional[pd.Series] = None  # |сила| в 0..1 (опц.)
    note: str = ""


@dataclass(slots=True)
class Indicator:
    """Базовый класс индикатора."""

    name: str
    group: str  # trend | momentum | volatility | volume | structure | patterns
    weight: float = 1.0  # вклад в группу
    description: str = ""
    fn: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None
    signal_fn: Optional[Callable[[pd.DataFrame], "SignalSeries"]] = None

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Добавляет колонки индикатора в df. Должна быть детерминированной."""
        if self.fn is None:
            return df
        return self.fn(df)

    def signal(self, df: pd.DataFrame) -> "SignalSeries":
        """
        Возвращает -1/0/+1 серию. По умолчанию — нули.
        Конкретные индикаторы передают signal_fn в конструктор.
        """
        if self.signal_fn is None:
            return SignalSeries(name=self.name, series=pd.Series(0, index=df.index))
        return self.signal_fn(df)


# ── Helpers ──────────────────────────────────────────────────


def _sign(series: pd.Series) -> pd.Series:
    """Чистый знак: -1, 0, +1 с защитой от NaN."""
    s = series.astype(float)
    out = pd.Series(0, index=s.index, dtype=float)
    out[s > 0] = 1
    out[s < 0] = -1
    return out


def group_signal(indicators: List[Indicator], df: pd.DataFrame, group: str) -> SignalSeries:
    """
    Схлопывает индикаторы одной группы в один Series.

    Урок BRAIN.md п.0d: тренд на 4 ТФ и RSI на 3 ТФ — это одно наблюдение,
    а не 7. Поэтому мы группируем по СЕМАНТИКЕ (тренд, моментум и т.д.),
    а не по таймфреймам.
    """
    members = [i for i in indicators if i.group == group]
    if not members:
        return SignalSeries(name=f"{group}_signal", series=pd.Series(0, index=df.index))

    series_list: List[pd.Series] = []
    weights: List[float] = []
    raw_contributions: List[str] = []
    for ind in members:
        sig = ind.signal(df)
        if sig.series is None or len(sig.series) == 0:
            continue
        s = sig.series.fillna(0).astype(float)
        # Нормируем по силе, если есть.
        if sig.strength is not None:
            s = s * sig.strength.fillna(0).clip(0, 1)
        series_list.append(s)
        weights.append(ind.weight)
        raw_contributions.append(ind.name)

    if not series_list:
        return SignalSeries(name=f"{group}_signal", series=pd.Series(0, index=df.index))

    w = np.array(weights, dtype=float)
    w = w / max(w.sum(), 1e-9)
    stacked = pd.concat(series_list, axis=1).fillna(0)
    avg = (stacked.values * w).sum(axis=1)
    avg = pd.Series(avg, index=df.index).clip(-1, 1)
    return SignalSeries(
        name=f"{group}_signal",
        series=_sign(avg),
        raw=avg,
        note=",".join(raw_contributions),
    )


def attach_signals(df: pd.DataFrame, indicators: List[Indicator]) -> pd.DataFrame:
    """
    Прогоняет все индикаторы + добавляет групповые сигналы.

    Возвращает копию df с колонками `<group>_signal`.
    """
    out = df.copy()
    # Сначала вычислим индикаторы (могут понадобиться их сырые колонки).
    for ind in indicators:
        try:
            out = ind.compute(out)
        except Exception:  # noqa: BLE001
            # Не роняем весь скан из-за одного сломанного индикатора.
            continue
    # Теперь групповые сигналы.
    for group in {i.group for i in indicators}:
        gs = group_signal(indicators, out, group)
        out[gs.name] = gs.series
        if gs.raw is not None:
            out[f"{gs.name}_raw"] = gs.raw
    return out
