"""
Свечные паттерны: упрощённые формы классических фигур.

Используем как «подтверждение», а не основу сигнала.
"""
from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from ..base import Indicator, SignalSeries, _sign


def hammer_shooting(period: int = 14) -> Indicator:
    """Hammer (бычий) / Shooting Star (медвежий)."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        body = (out["close"] - out["open"]).abs()
        upper_wick = out["high"] - out[["close", "open"]].max(axis=1)
        lower_wick = out[["close", "open"]].min(axis=1) - out["low"]
        out["body"] = body
        out["upper_wick"] = upper_wick
        out["lower_wick"] = lower_wick
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        if "body" not in df.columns:
            return SignalSeries(name="hammer", series=pd.Series(0, index=df.index))
        body = df["body"]
        up_w = df["upper_wick"]
        low_w = df["lower_wick"]
        # Hammer: маленькое тело, длинный нижний фитиль (>= 2*body), короткий верх (<= body).
        bull = (low_w >= body * 2) & (up_w <= body)
        # Shooting Star: маленькое тело, длинный верхний фитиль, короткий низ.
        bear = (up_w >= body * 2) & (low_w <= body)
        s = pd.Series(0, index=df.index, dtype=float)
        s[bull] = 1
        s[bear] = -1
        return SignalSeries(name="hammer", series=s)

    return Indicator(
        name="hammer_shooting",
        group="patterns",
        weight=0.4,
        description="Hammer (bullish) / Shooting Star (bearish)",
        fn=compute,
        signal_fn=signal,
    )


def engulfing() -> Indicator:
    """Engulfing pattern."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["prev_open"] = out["open"].shift(1)
        out["prev_close"] = out["close"].shift(1)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        if "prev_open" not in df.columns:
            return SignalSeries(name="engulfing", series=pd.Series(0, index=df.index))
        co = df["close"] - df["open"]
        po = df["prev_close"] - df["prev_open"]
        bull = (co > 0) & (po < 0) & (df["open"] < df["prev_close"]) & (df["close"] > df["prev_open"])
        bear = (co < 0) & (po > 0) & (df["open"] > df["prev_close"]) & (df["close"] < df["prev_open"])
        s = pd.Series(0, index=df.index, dtype=float)
        s[bull] = 1
        s[bear] = -1
        return SignalSeries(name="engulfing", series=s)

    return Indicator(
        name="engulfing",
        group="patterns",
        weight=0.4,
        description="Engulfing pattern",
        fn=compute,
        signal_fn=signal,
    )


REGISTRY: List[Indicator] = [
    hammer_shooting(),
    engulfing(),
]
