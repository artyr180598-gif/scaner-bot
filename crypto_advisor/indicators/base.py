"""
Базовый класс индикатора.

Freqtrade-style: populate_indicators() — чистая функция (df) -> df.
Но мы оборачиваем в dataclass для регистрации + метаданных (group, weight).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass(slots=True)
class SignalSeries:
    """Серия сигналов одного индикатора: -1, 0, +1 (длинный/нейтрально/короткий)."""

    name: str
    series: pd.Series
    raw: Optional[pd.Series] = None
    strength: Optional[pd.Series] = None
    note: str = ""


@dataclass(slots=True)
class Indicator:
    """Индикатор с метаданными (group, weight) и функциями compute/signal."""

    name: str
    group: str
    weight: float = 1.0
    description: str = ""
    fn: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None
    signal_fn: Optional[Callable[[pd.DataFrame], "SignalSeries"]] = None

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.fn is None:
            return df
        return self.fn(df)

    def signal(self, df: pd.DataFrame) -> "SignalSeries":
        if self.signal_fn is None:
            return SignalSeries(name=self.name, series=pd.Series(0, index=df.index))
        return self.signal_fn(df)


def _sign(series: pd.Series) -> pd.Series:
    """Чистый знак: -1, 0, +1 с защитой от NaN."""
    s = series.astype(float)
    out = pd.Series(0, index=s.index, dtype=float)
    out[s > 0] = 1
    out[s < 0] = -1
    return out


def group_signal(indicators: List[Indicator], df: pd.DataFrame, group: str) -> SignalSeries:
    """Схлопывает индикаторы одной группы в один Series (-1/0/+1)."""
    members = [i for i in indicators if i.group == group]
    if not members:
        return SignalSeries(name=f"{group}_signal", series=pd.Series(0, index=df.index))

    series_list: List[pd.Series] = []
    weights: List[float] = []
    for ind in members:
        sig = ind.signal(df)
        if sig.series is None or len(sig.series) == 0:
            continue
        s = sig.series.fillna(0).astype(float)
        if sig.strength is not None:
            s = s * sig.strength.fillna(0).clip(0, 1)
        series_list.append(s)
        weights.append(ind.weight)

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
    )


def attach_signals(df: pd.DataFrame, indicators: List[Indicator]) -> pd.DataFrame:
    """Прогоняет все индикаторы + добавляет групповые сигналы в df."""
    out = df.copy()
    for ind in indicators:
        try:
            out = ind.compute(out)
        except Exception:  # noqa: BLE001
            continue
    for group in {i.group for i in indicators}:
        gs = group_signal(indicators, out, group)
        out[gs.name] = gs.series
        if gs.raw is not None:
            out[f"{gs.name}_raw"] = gs.raw
    return out
