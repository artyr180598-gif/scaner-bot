"""Индикаторы объёма: OBV, Volume Z-score, Price-Volume confirm."""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from .base import Indicator, SignalSeries, _sign


def obv() -> Indicator:
    """On-Balance Volume."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        sign = _sign(out["close"].diff()).fillna(0)
        out["obv"] = (sign * out["volume"]).cumsum()
        out["obv_slope"] = out["obv"].diff(5).fillna(0)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        slope = df.get("obv_slope")
        if slope is None:
            return SignalSeries(name="obv", series=pd.Series(0, index=df.index))
        s = _sign(slope)
        return SignalSeries(name="obv", series=s, raw=slope)

    return Indicator(
        name="obv",
        group="volume",
        weight=0.7,
        description="OBV slope (5-bar)",
        fn=compute,
        signal_fn=signal,
    )


def volume_zscore(window: int = 20) -> Indicator:
    """Аномальный объём: |z| > 2 (считается по ПРЕДЫДУЩИМ барам)."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        prev = out["volume"].shift(1)
        mean = prev.rolling(window).mean()
        std = prev.rolling(window).std()
        out["vol_z"] = ((out["volume"] - mean) / std.replace(0, np.nan)).fillna(0)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        z = df.get("vol_z")
        if z is None:
            return SignalSeries(name="vol_z", series=pd.Series(0, index=df.index))
        s = pd.Series(0, index=z.index, dtype=float)
        s[z > 2.0] = 1
        s[z < -2.0] = -1
        strength = z.abs().clip(0, 4) / 4.0
        return SignalSeries(name="vol_z", series=s, raw=z, strength=strength)

    return Indicator(
        name=f"volume_zscore_{window}",
        group="volume",
        weight=0.8,
        description="Volume z-score (anomaly, no-lookahead)",
        fn=compute,
        signal_fn=signal,
    )


def price_volume_confirm() -> Indicator:
    """Подтверждение движения объёмом: цена вверх + объём вверх = +1."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        pc = _sign(out["close"].diff()).fillna(0)
        vc = _sign(out["volume"].diff()).fillna(0)
        out["pv_confirm"] = pc * vc
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        pvc = df.get("pv_confirm")
        if pvc is None:
            return SignalSeries(name="pv_confirm", series=pd.Series(0, index=df.index))
        return SignalSeries(name="pv_confirm", series=pvc, raw=pvc.astype(float))

    return Indicator(
        name="price_volume_confirm",
        group="volume",
        weight=0.5,
        description="Price move confirmed by volume",
        fn=compute,
        signal_fn=signal,
    )


REGISTRY: List[Indicator] = [
    obv(),
    volume_zscore(),
    price_volume_confirm(),
]
