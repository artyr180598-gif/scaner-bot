"""Индикаторы моментума: RSI, MACD, Stochastic."""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from .base import Indicator, SignalSeries, _sign


def rsi(period: int = 14) -> Indicator:
    """RSI — Relative Strength Index."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        delta = out["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        out["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        r = df.get("rsi")
        if r is None:
            return SignalSeries(name="rsi", series=pd.Series(0, index=df.index))
        s = pd.Series(0, index=r.index, dtype=float)
        s[r < 30] = 1
        s[r > 70] = -1
        strength_long = ((50 - r) / 20).clip(0, 1)
        strength_short = ((r - 50) / 20).clip(0, 1)
        strength = pd.Series(0.0, index=r.index)
        strength[r < 50] = strength_long[r < 50]
        strength[r > 50] = strength_short[r > 50]
        return SignalSeries(name="rsi", series=s, raw=r, strength=strength)

    return Indicator(
        name=f"rsi_{period}",
        group="momentum",
        weight=0.9,
        description="RSI Wilder",
        fn=compute,
        signal_fn=signal,
    )


def macd(fast: int = 12, slow: int = 26, signal_p: int = 9) -> Indicator:
    """MACD — схождение/расхождение EMA."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        ema_fast = out["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = out["close"].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        sig = macd_line.ewm(span=signal_p, adjust=False).mean()
        out["macd"] = macd_line
        out["macd_signal"] = sig
        out["macd_hist"] = macd_line - sig
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        h = df.get("macd_hist")
        if h is None:
            return SignalSeries(name="macd", series=pd.Series(0, index=df.index))
        s = _sign(h)
        strength = (h.abs() / df["close"]).clip(0, 0.02) / 0.02
        return SignalSeries(name="macd", series=s, raw=h, strength=strength)

    return Indicator(
        name=f"macd_{fast}_{slow}_{signal_p}",
        group="momentum",
        weight=0.9,
        description="MACD line/signal/histogram",
        fn=compute,
        signal_fn=signal,
    )


def stochastic(k: int = 14, d: int = 3, smooth: int = 3) -> Indicator:
    """Stochastic Oscillator (%K / %D)."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        low_k = out["low"].rolling(k).min()
        high_k = out["high"].rolling(k).max()
        stoch_k = 100 * (out["close"] - low_k) / (high_k - low_k).replace(0, np.nan)
        stoch_k = stoch_k.rolling(smooth).mean()
        stoch_d = stoch_k.rolling(d).mean()
        out["stoch_k"] = stoch_k.fillna(50)
        out["stoch_d"] = stoch_d.fillna(50)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        k_s = df.get("stoch_k")
        d_s = df.get("stoch_d")
        if k_s is None or d_s is None:
            return SignalSeries(name="stoch", series=pd.Series(0, index=df.index))
        cross = _sign(k_s - d_s)
        zone = pd.Series(0, index=k_s.index, dtype=float)
        zone[k_s < 20] = 1
        zone[k_s > 80] = -1
        s = pd.Series(0, index=k_s.index, dtype=int)
        s[(cross > 0) & (zone >= 0)] = 1
        s[(cross < 0) & (zone <= 0)] = -1
        strength = (k_s - 50).abs() / 50.0
        return SignalSeries(name="stoch", series=s, raw=k_s, strength=strength)

    return Indicator(
        name=f"stoch_{k}_{d}_{smooth}",
        group="momentum",
        weight=0.7,
        description="Stochastic %K/%D with zone filter",
        fn=compute,
        signal_fn=signal,
    )


REGISTRY: List[Indicator] = [
    rsi(14),
    macd(),
    stochastic(),
]
