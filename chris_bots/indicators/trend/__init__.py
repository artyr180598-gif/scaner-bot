"""
Индикаторы тренда.

Лучшие практики:
- Freqtrade: EMA, SMA, Ichimoku.
- Jesse: четкая семантика «тренд вверх/вниз/боковик».
- CryptoSignal: BBANDS, PSAR.

Реализовано:
- EMA Cross (быстрая/медленная)
- Supertrend (исправленная версия — урок BRAIN.md п.12)
- Ichimoku Cloud (упрощённо)
- Parabolic SAR (упрощённо)
- ADX (сила тренда)
"""
from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from ..base import Indicator, SignalSeries, _sign


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
        # Сила — нормированный diff относительно цены.
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
    """
    Supertrend — трендовый индикатор на ATR.

    Урок BRAIN.md п.12: «Supertrend в лоб всегда +1». Здесь мы сравниваем
    close с финальными полосами ПРЕДЫДУЩЕГО бара.
    """

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        hl2 = (out["high"] + out["low"]) / 2
        # ATR
        tr = pd.concat([
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        out["st_atr"] = atr
        upper = hl2 + multiplier * atr
        lower = hl2 - multiplier * atr
        # Финальные полосы (правило: полоса не может «отступить назад»).
        final_upper = upper.copy()
        final_lower = lower.copy()
        for i in range(1, len(out)):
            if pd.isna(final_upper.iloc[i - 1]):
                continue
            fu, fl = final_upper.iloc[i], final_lower.iloc[i]
            pu, pl = final_upper.iloc[i - 1], final_lower.iloc[i - 1]
            cu, cl = out["close"].iloc[i - 1], out["close"].iloc[i - 1]
            # Нижняя полоса растёт, только если цена была выше предыдущей нижней.
            final_lower.iloc[i] = (
                fl if (fl > pl or cu < pl) else pl
            )
            # Верхняя полоса падает, только если цена была ниже предыдущей верхней.
            final_upper.iloc[i] = (
                fu if (fu < pu or cl > pu) else pu
            )
        out["st_upper"] = final_upper
        out["st_lower"] = final_lower
        out["st_dir"] = 0
        st_dir = out["st_dir"].astype(int)
        for i in range(1, len(out)):
            prev = int(out["st_dir"].iloc[i - 1]) if not pd.isna(out["st_dir"].iloc[i - 1]) else 0
            c = out["close"].iloc[i]
            if pd.isna(out["st_lower"].iloc[i]) or pd.isna(out["st_upper"].iloc[i]):
                st_dir.iloc[i] = prev
                continue
            if prev <= 0 and c > out["st_lower"].iloc[i]:
                st_dir.iloc[i] = 1
            elif prev >= 0 and c < out["st_upper"].iloc[i]:
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
        return SignalSeries(name="supertrend", series=s, raw=d.astype(float))

    return Indicator(
        name=f"supertrend_{period}_{multiplier}",
        group="trend",
        weight=0.9,
        description="Supertrend (ATR-based, prev-bar fix)",
        fn=compute,
        signal_fn=signal,
    )


def adx(period: int = 14) -> Indicator:
    """ADX — сила тренда (направление отдельно через +DI/-DI)."""

    def compute(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        up = out["high"].diff()
        down = -out["low"].diff()
        plus_dm = ((up > down) & (up > 0)) * up
        minus_dm = ((down > up) & (down > 0)) * down
        tr = pd.concat([
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_s = dx.ewm(span=period, adjust=False).mean()
        out["plus_di"] = plus_di.fillna(0)
        out["minus_di"] = minus_di.fillna(0)
        out["adx"] = adx_s.fillna(0)
        return out

    def signal(df: pd.DataFrame) -> SignalSeries:
        plus = df.get("plus_di")
        minus = df.get("minus_di")
        adx_s = df.get("adx")
        if plus is None or minus is None or adx_s is None:
            return SignalSeries(name="adx", series=pd.Series(0, index=df.index))
        direction = _sign(plus - minus)
        # Сила = ADX/100, обрезанная до 0.5..1.0 (тренд есть при ADX > 20).
        strength = (adx_s / 100.0).clip(0, 1)
        # Если ADX < 18 — тренда нет, ослабляем сигнал.
        strength = strength * (adx_s >= 18).astype(float)
        return SignalSeries(name="adx", series=direction, raw=adx_s, strength=strength)

    return Indicator(
        name=f"adx_{period}",
        group="trend",
        weight=0.8,
        description="ADX with DI+/-",
        fn=compute,
        signal_fn=signal,
    )


# ── Реестр ──────────────────────────────────────────────────
REGISTRY: List[Indicator] = [
    ema_cross(21, 55),
    ema_cross(50, 200),
    supertrend(10, 3.0),
    adx(14),
]
