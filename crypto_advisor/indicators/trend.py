"""Индикаторы тренда: EMA Cross, Supertrend, ADX."""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from .base import Indicator, SignalSeries, _sign


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def ema_cross(period_fast: int = 21, period_slow: int = 55) -> Indicator:
    """Пересечение EMA — классика тренда."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out[f"ema{period_fast}"] = ema(out["close"], period_fast)
        out[f"ema{period_slow}"] = ema(out["close"], period_slow)
        out["ema_cross_diff"] = out[f"ema{period_fast}"] - out[f"ema{period_slow}"]
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        diff = df.get("ema_cross_diff")
        if diff is None:
            return SignalSeries(name="ema_cross", series=pd.Series(0, index=df.index))
        s = _sign(diff)
        strength = (diff.abs() / df["close"]).clip(0, 0.05) / 0.05
        return SignalSeries(name="ema_cross", series=s, raw=diff, strength=strength)

    return Indicator(
        name=f"ema_cross_{period_fast}_{period_slow}",
        group="trend",
        weight=1.0,
        description=f"EMA fast/slow cross ({period_fast}/{period_slow})",
        fn=compute,
        signal_fn=signal,
    )


def supertrend(period: int = 10, multiplier: float = 3.0) -> Indicator:
    """Supertrend — трендовый индикатор на ATR (без look-ahead)."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        hl2 = (out["high"] + out["low"]) / 2
        tr = pd.concat([
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        out["st_atr"] = atr
        upper = hl2 + multiplier * atr
        lower = hl2 - multiplier * atr
        final_upper = upper.copy()
        final_lower = lower.copy()
        for i in range(1, len(out)):
            if pd.isna(final_upper.iloc[i - 1]):
                continue
            fu, fl = final_upper.iloc[i], final_lower.iloc[i]
            pu, pl = final_upper.iloc[i - 1], final_lower.iloc[i - 1]
            cu, cl = out["close"].iloc[i - 1], out["close"].iloc[i - 1]
            final_lower.iloc[i] = fl if (fl > pl or cu < pl) else pl
            final_upper.iloc[i] = fu if (fu < pu or cl > pu) else pu
        out["st_upper"] = final_upper
        out["st_lower"] = final_lower
        st_dir = pd.Series(0, index=out.index, dtype=int)
        for i in range(1, len(out)):
            prev = int(st_dir.iloc[i - 1])
            c = out["close"].iloc[i]
            if pd.isna(final_lower.iloc[i]) or pd.isna(final_upper.iloc[i]):
                st_dir.iloc[i] = prev
                continue
            if prev <= 0 and c > final_lower.iloc[i]:
                st_dir.iloc[i] = 1
            elif prev >= 0 and c < final_upper.iloc[i]:
                st_dir.iloc[i] = -1
            else:
                st_dir.iloc[i] = prev
        out["st_dir"] = st_dir
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        d = df.get("st_dir")
        if d is None:
            return SignalSeries(name="supertrend", series=pd.Series(0, index=df.index))
        s = d.fillna(0).astype(int).clip(-1, 1)
        strength = pd.Series(1.0, index=df.index)
        return SignalSeries(name="supertrend", series=s, raw=s, strength=strength)

    return Indicator(
        name=f"supertrend_{period}_{multiplier}",
        group="trend",
        weight=1.1,
        description=f"Supertrend period={period}, mult={multiplier}",
        fn=compute,
        signal_fn=signal,
    )


def adx(period: int = 14) -> Indicator:
    """ADX — сила тренда (направления не даёт, только силу)."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        plus_dm = out["high"].diff().clip(lower=0) * -1  # placeholder below
        up_move = out["high"].diff()
        down_move = -out["low"].diff()
        plus_dm = up_move.where(up_move > down_move, 0.0)
        minus_dm = down_move.where(down_move > up_move, 0.0)
        tr = pd.concat([
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        out["adx"] = dx.ewm(span=period, adjust=False).mean().fillna(20)
        out["plus_di"] = plus_di.fillna(0)
        out["minus_di"] = minus_di.fillna(0)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        if "plus_di" not in df.columns:
            return SignalSeries(name="adx", series=pd.Series(0, index=df.index))
        s = _sign(df["plus_di"] - df["minus_di"])
        strength = (df["adx"] / 50.0).clip(0, 1)
        return SignalSeries(name="adx", series=s, raw=df["adx"], strength=strength)

    return Indicator(
        name=f"adx_{period}",
        group="trend",
        weight=0.7,
        description="ADX direction + strength",
        fn=compute,
        signal_fn=signal,
    )


REGISTRY: List[Indicator] = [
    ema_cross(),
    supertrend(),
    adx(),
]
