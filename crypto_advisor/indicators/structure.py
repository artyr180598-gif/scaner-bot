"""Структура рынка: свинги, пробои диапазона."""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from .base import Indicator, SignalSeries, _sign


def swing_structure(lookback: int = 5) -> Indicator:
    """Higher Highs / Higher Lows (тренд вверх) → +1, LH/LL → -1."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        highs = out["high"].values
        lows = out["low"].values
        hh = np.zeros(len(out), dtype=int)
        ll = np.zeros(len(out), dtype=int)
        for i in range(lookback, len(out)):
            window = slice(i - lookback, i)
            if highs[i] >= highs[window].max() and highs[i] > highs[i - 1]:
                hh[i] = 1
            if lows[i] <= lows[window].min() and lows[i] < lows[i - 1]:
                ll[i] = 1
        out["swing_hh"] = hh
        out["swing_ll"] = ll
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        hh = df.get("swing_hh")
        ll = df.get("swing_ll")
        if hh is None or ll is None:
            return SignalSeries(name="swings", series=pd.Series(0, index=df.index))
        s = pd.Series(0, index=df.index, dtype=float)
        s[hh > ll] = 1
        s[ll > hh] = -1
        return SignalSeries(name="swings", series=s, raw=s)

    return Indicator(
        name=f"swing_structure_{lookback}",
        group="structure",
        weight=0.7,
        description="Higher Highs / Higher Lows vs LH/LL",
        fn=compute,
        signal_fn=signal,
    )


def breakout(period: int = 20) -> Indicator:
    """Пробой N-периодного диапазона high/low."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["donchian_high"] = out["high"].rolling(period).max().shift(1)
        out["donchian_low"] = out["low"].rolling(period).min().shift(1)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        h = df.get("donchian_high")
        l = df.get("donchian_low")
        if h is None or l is None:
            return SignalSeries(name="breakout", series=pd.Series(0, index=df.index))
        s = pd.Series(0, index=df.index, dtype=float)
        s[df["close"] > h] = 1
        s[df["close"] < l] = -1
        return SignalSeries(name="breakout", series=s)

    return Indicator(
        name=f"breakout_{period}",
        group="structure",
        weight=0.6,
        description="Donchian breakout",
        fn=compute,
        signal_fn=signal,
    )


REGISTRY: List[Indicator] = [
    swing_structure(5),
    breakout(20),
]
