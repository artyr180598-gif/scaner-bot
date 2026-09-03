"""Индикаторы волатильности: ATR, Bollinger Bands."""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from .base import Indicator, SignalSeries, _sign


def atr(period: int = 14) -> Indicator:
    """Average True Range."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        tr = pd.concat([
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        out["atr"] = tr.ewm(span=period, adjust=False).mean().fillna(0)
        out["atr_pct"] = (out["atr"] / out["close"]).fillna(0) * 100
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        if "atr" not in df.columns:
            return SignalSeries(name="atr", series=pd.Series(0, index=df.index))
        return SignalSeries(
            name="atr",
            series=pd.Series(0, index=df.index),
            raw=df["atr_pct"],
            strength=pd.Series(1.0, index=df.index),
        )

    return Indicator(
        name=f"atr_{period}",
        group="volatility",
        weight=0.6,
        description="ATR / ATR%",
        fn=compute,
        signal_fn=signal,
    )


def bollinger(period: int = 20, std: float = 2.0) -> Indicator:
    """Bollinger Bands — волатильность + перекупленность/перепроданность."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        ma = out["close"].rolling(period).mean()
        sd = out["close"].rolling(period).std()
        out["bb_mid"] = ma
        out["bb_upper"] = ma + std * sd
        out["bb_lower"] = ma - std * sd
        out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"]
        out["bb_pct"] = (out["close"] - out["bb_lower"]) / (
            out["bb_upper"] - out["bb_lower"]
        ).replace(0, np.nan)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        p = df.get("bb_pct")
        if p is None:
            return SignalSeries(name="bollinger", series=pd.Series(0, index=df.index))
        s = pd.Series(0, index=p.index, dtype=float)
        s[p < 0.1] = 1
        s[p > 0.9] = -1
        strength = (p - 0.5).abs() * 2
        return SignalSeries(name="bollinger", series=s, raw=p, strength=strength.clip(0, 1))

    return Indicator(
        name=f"bb_{period}_{std}",
        group="volatility",
        weight=0.6,
        description="Bollinger Bands position",
        fn=compute,
        signal_fn=signal,
    )


REGISTRY: List[Indicator] = [
    atr(14),
    bollinger(),
]
