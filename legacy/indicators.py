"""
indicators.py — чистая математика технических индикаторов (без сети и зависимостей).

Почему без numpy/pandas: прод-контур (`requirements.txt`) держим лёгким — на
Railway крутится worker, а pandas тянет 40+ МБ. Все расчёты здесь — обычный
Python на списках float, объёмы данных маленькие (сотни свечей).

Каждая функция возвращает СПИСОК той же длины, что и вход, где невычислимые
позиции = None. Это принципиально: движок сигнала должен видеть, где данных
не хватило, и честно снижать «уверенность анализа», а не подставлять нули.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

__all__ = [
    "sma",
    "ema",
    "rsi",
    "macd",
    "stochastic",
    "true_range",
    "atr",
    "bollinger",
    "obv",
    "linreg_slope",
    "percentile_rank",
    "swing_highs",
    "swing_lows",
]

Num = Optional[float]


# ---------------------------------------------------------------------------
# Скользящие средние
# ---------------------------------------------------------------------------

def sma(values: Sequence[float], period: int) -> list[Num]:
    """Простая скользящая средняя."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[Num] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[Num]:
    """
    Экспоненциальная скользящая. Стартует от SMA первых `period` значений —
    так делает большинство терминалов (TradingView), чтобы не «разгонять»
    среднюю от первой свечи.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[Num] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Моментум
# ---------------------------------------------------------------------------

def rsi(values: Sequence[float], period: int = 14) -> list[Num]:
    """RSI Уайлдера (сглаживание Wilder'а, не простое среднее)."""
    out: list[Num] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[Num], list[Num], list[Num]]:
    """MACD → (линия, сигнальная, гистограмма)."""
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    macd_line: list[Num] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_line, slow_line)
    ]
    # сигнальная считается по непрерывному хвосту MACD-линии
    valid = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[Num] = [None] * len(values)
    hist: list[Num] = [None] * len(values)
    if len(valid) >= signal:
        raw = [v for _, v in valid]
        sig = ema(raw, signal)
        for (idx, _), s in zip(valid, sig):
            signal_line[idx] = s
            if s is not None and macd_line[idx] is not None:
                hist[idx] = macd_line[idx] - s
    return macd_line, signal_line, hist


def stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    k_period: int = 14,
    d_period: int = 3,
    smooth_k: int = 3,
) -> tuple[list[Num], list[Num]]:
    """Стохастик %K (сглаженный) и %D."""
    n = len(closes)
    raw_k: list[Num] = [None] * n
    for i in range(n):
        if i < k_period - 1:
            continue
        window_high = max(highs[i - k_period + 1: i + 1])
        window_low = min(lows[i - k_period + 1: i + 1])
        span = window_high - window_low
        raw_k[i] = 50.0 if span <= 0 else (closes[i] - window_low) / span * 100.0
    k_line = _smooth_optional(raw_k, smooth_k)
    d_line = _smooth_optional(k_line, d_period)
    return k_line, d_line


def _smooth_optional(series: Sequence[Num], period: int) -> list[Num]:
    if period <= 1:
        return list(series)
    out: list[Num] = [None] * len(series)
    for i in range(len(series)):
        window = series[max(0, i - period + 1): i + 1]
        if len(window) < period or any(v is None for v in window):
            continue
        out[i] = sum(float(v) for v in window) / period  # type: ignore[arg-type]
    return out


# ---------------------------------------------------------------------------
# Волатильность
# ---------------------------------------------------------------------------

def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[Num]:
    out: list[Num] = [None] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            out[i] = highs[i] - lows[i]
            continue
        prev_close = closes[i - 1]
        out[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[Num]:
    """ATR Уайлдера."""
    tr = true_range(highs, lows, closes)
    out: list[Num] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    seed = sum(float(v) for v in tr[1: period + 1]) / period  # type: ignore[arg-type]
    out[period] = seed
    prev = seed
    for i in range(period + 1, len(closes)):
        prev = (prev * (period - 1) + float(tr[i])) / period  # type: ignore[arg-type]
        out[i] = prev
    return out


def bollinger(
    values: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[list[Num], list[Num], list[Num]]:
    """Полосы Боллинджера → (верх, середина, низ)."""
    mid = sma(values, period)
    upper: list[Num] = [None] * len(values)
    lower: list[Num] = [None] * len(values)
    for i in range(len(values)):
        if mid[i] is None:
            continue
        window = values[i - period + 1: i + 1]
        mean = float(mid[i])  # type: ignore[arg-type]
        var = sum((v - mean) ** 2 for v in window) / period
        sd = math.sqrt(var)
        upper[i] = mean + mult * sd
        lower[i] = mean - mult * sd
    return upper, mid, lower


# ---------------------------------------------------------------------------
# Объём и вспомогательное
# ---------------------------------------------------------------------------

def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """On-Balance Volume — накопленный «направленный» объём."""
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - volumes[i]
        else:
            out[i] = out[i - 1]
    return out


def linreg_slope(values: Sequence[float]) -> Optional[float]:
    """
    Наклон линейной регрессии по индексу (единиц значения за бар).
    Нужен, чтобы отличать «RSI растёт» от «RSI просто высокий».
    """
    n = len(values)
    if n < 3:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return None
    return num / den


def percentile_rank(values: Sequence[float], value: float) -> Optional[float]:
    """Процент значений выборки, которые НЕ больше `value` (0..100)."""
    if not values:
        return None
    below = sum(1 for v in values if v <= value)
    return below / len(values) * 100.0


def swing_highs(highs: Sequence[float], left: int = 2, right: int = 2) -> list[int]:
    """Индексы локальных максимумов (фракталы) — сырьё для уровней."""
    out: list[int] = []
    for i in range(left, len(highs) - right):
        window = highs[i - left: i + right + 1]
        if highs[i] == max(window) and window.count(highs[i]) == 1:
            out.append(i)
    return out


def swing_lows(lows: Sequence[float], left: int = 2, right: int = 2) -> list[int]:
    """Индексы локальных минимумов (фракталы)."""
    out: list[int] = []
    for i in range(left, len(lows) - right):
        window = lows[i - left: i + right + 1]
        if lows[i] == min(window) and window.count(lows[i]) == 1:
            out.append(i)
    return out
