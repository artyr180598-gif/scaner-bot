"""
app/indicators/core.py — математика технических индикаторов.

Почему своя реализация, а не `ta`/`pandas-ta`:
  * обе библиотеки тянут тяжёлые зависимости и регулярно ломаются на новых
    версиях pandas/numpy (у pandas 3.x уже поменялись дефолты rolling/ewm);
  * нам нужны ровно те варианты формул, что используются в бектесте
    (Wilder RSI/ATR, «честный» MFI и т.д.) — иначе live и бектест разъезжаются;
  * все функции векторизованы и НЕ заглядывают в будущее: значение в баре i
    зависит только от баров ≤ i. Это проверяется тестом
    ``tests/test_indicators.py::test_no_lookahead``.

Соглашение: на вход — ``pandas.Series``/``DataFrame``, на выходе — ``Series``
той же длины (первые значения могут быть NaN).
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import numpy as np
import pandas as pd

__all__ = [
    "sma", "ema", "wma", "rma", "hma", "kama",
    "rsi", "stoch_rsi", "macd", "true_range", "atr", "natr",
    "bollinger", "keltner", "stochastic", "adx", "obv", "mfi", "cci",
    "williams_r", "roc", "momentum", "cmo", "vwap", "typical_price",
    "zscore", "percentile_rank", "donchian", "highest", "lowest",
    "crossover", "crossunder", "linear_slope", "r_squared", "supertrend",
    "safe_float", "last_valid",
]


# ---------------------------------------------------------------------------
# Базовые скользящие
# ---------------------------------------------------------------------------

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(int(period), min_periods=int(period)).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """EMA с ``adjust=False`` — как в торговых терминалах."""
    return series.ewm(span=int(period), adjust=False, min_periods=int(period)).mean()


def wma(series: pd.Series, period: int) -> pd.Series:
    period = int(period)
    weights = np.arange(1, period + 1, dtype="float64")

    def _wma(window: np.ndarray) -> float:
        return float(np.dot(window, weights) / weights.sum())

    return series.rolling(period, min_periods=period).apply(_wma, raw=True)


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's Moving Average (используется в RSI/ATR/ADX)."""
    return series.ewm(alpha=1.0 / int(period), adjust=False,
                      min_periods=int(period)).mean()


def hma(series: pd.Series, period: int) -> pd.Series:
    """Hull MA — меньше лаг, полезен для оценки локального направления."""
    period = max(2, int(period))
    half = wma(series, max(1, period // 2))
    full = wma(series, period)
    raw = 2 * half - full
    return wma(raw, max(1, int(math.sqrt(period))))


def kama(series: pd.Series, period: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """Адаптивная скользящая Кауфмана: быстрая в тренде, медленная во флэте."""
    period, fast, slow = int(period), int(fast), int(slow)
    change = series.diff(period).abs()
    volatility = series.diff().abs().rolling(period, min_periods=period).sum()
    er = (change / volatility.replace(0, np.nan)).fillna(0.0)
    sc = (er * (2 / (fast + 1) - 2 / (slow + 1)) + 2 / (slow + 1)) ** 2
    out = pd.Series(np.nan, index=series.index, dtype="float64")
    values = series.to_numpy(dtype="float64")
    sc_arr = sc.to_numpy(dtype="float64")
    prev = np.nan
    for i in range(len(values)):
        if np.isnan(sc_arr[i]) or np.isnan(values[i]):
            continue
        prev = values[i] if np.isnan(prev) else prev + sc_arr[i] * (values[i] - prev)
        out.iloc[i] = prev
    return out


# ---------------------------------------------------------------------------
# Осцилляторы
# ---------------------------------------------------------------------------

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Классический RSI Уайлдера."""
    period = int(period)
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # Когда потерь нет вовсе — RSI = 100, а не NaN.
    out = out.where(avg_loss != 0, 100.0)
    return out.where(avg_gain.notna())


def stoch_rsi(series: pd.Series, rsi_period: int = 14, period: int = 14,
              k_period: int = 3, d_period: int = 3) -> Tuple[pd.Series, pd.Series]:
    base = rsi(series, rsi_period)
    lo = base.rolling(period, min_periods=period).min()
    hi = base.rolling(period, min_periods=period).max()
    raw = (base - lo) / (hi - lo).replace(0, np.nan) * 100
    k = raw.rolling(k_period, min_periods=1).mean()
    d = k.rolling(d_period, min_periods=1).mean()
    return k, d


def macd(series: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(series, fast) - ema(series, slow)
    sig = line.ewm(span=int(signal), adjust=False, min_periods=int(signal)).mean()
    return line, sig, line - sig


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               k_period: int = 14, d_period: int = 3,
               smooth: int = 3) -> Tuple[pd.Series, pd.Series]:
    """Slow Stochastic %K/%D (со сглаживанием, как в TradingView)."""
    k_period, d_period, smooth = int(k_period), int(d_period), int(smooth)
    ll = low.rolling(k_period, min_periods=k_period).min()
    hh = high.rolling(k_period, min_periods=k_period).max()
    raw = (close - ll) / (hh - ll).replace(0, np.nan) * 100
    k = raw.rolling(smooth, min_periods=1).mean()
    d = k.rolling(d_period, min_periods=1).mean()
    return k, d


def cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
    tp = typical_price(high, low, close)
    ma = sma(tp, period)
    md = tp.rolling(period, min_periods=period).apply(
        lambda w: np.mean(np.abs(w - np.mean(w))), raw=True)
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 14) -> pd.Series:
    hh = high.rolling(period, min_periods=period).max()
    ll = low.rolling(period, min_periods=period).min()
    return (hh - close) / (hh - ll).replace(0, np.nan) * -100


def roc(series: pd.Series, period: int = 10) -> pd.Series:
    prev = series.shift(int(period))
    return (series / prev.replace(0, np.nan) - 1) * 100


def momentum(series: pd.Series, period: int = 10) -> pd.Series:
    return series - series.shift(int(period))


def cmo(series: pd.Series, period: int = 14) -> pd.Series:
    """Chande Momentum Oscillator: «чистый» импульс без деления на потери."""
    delta = series.diff()
    up = delta.clip(lower=0).rolling(period, min_periods=period).sum()
    dn = (-delta.clip(upper=0)).rolling(period, min_periods=period).sum()
    total = (up + dn).replace(0, np.nan)
    return (up - dn) / total * 100


# ---------------------------------------------------------------------------
# Волатильность / диапазоны
# ---------------------------------------------------------------------------

def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), period)


def natr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR в процентах от цены — сравнимо между монетами."""
    a = atr(high, low, close, period)
    return a / close.replace(0, np.nan) * 100


def bollinger(series: pd.Series, period: int = 20,
              mult: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Возвращает (mid, upper, lower, width%). width = (upper-lower)/mid*100."""
    period = int(period)
    mid = sma(series, period)
    std = series.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    width = (upper - lower) / mid.replace(0, np.nan) * 100
    return mid, upper, lower, width


def keltner(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20,
            atr_period: int = 10, mult: float = 1.5
            ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = ema(close, period)
    a = atr(high, low, close, atr_period)
    return mid, mid + mult * a, mid - mult * a


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
        ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """ADX/+DI/-DI (Wilder). Возвращает (adx, plus_di, minus_di)."""
    period = int(period)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    tr = rma(true_range(high, low, close), period)
    plus_di = 100 * rma(plus_dm, period) / tr.replace(0, np.nan)
    minus_di = 100 * rma(minus_dm, period) / tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return rma(dx, period), plus_di, minus_di


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 10, mult: float = 3.0) -> Tuple[pd.Series, pd.Series]:
    """
    Supertrend: (линия, направление +1/-1). Удобный «тренд-фильтр».

    Реализация по каноническому алгоритму (pandas_ta / TradingView):
      * полосы «защёлкиваются» только в сторону тренда — верхняя может лишь
        опускаться, нижняя лишь подниматься;
      * смена направления проверяется по полосам ПРЕДЫДУЩЕГО бара, иначе
        полоса успевает «догнать» цену и тренд не переключается никогда
        (именно так выглядел баг первой версии этой функции: +1 на любом рынке).
    """
    a = atr(high, low, close, period)
    hl2 = (high + low) / 2
    upper = (hl2 + mult * a).to_numpy(dtype="float64")
    lower = (hl2 - mult * a).to_numpy(dtype="float64")
    close_arr = close.to_numpy(dtype="float64")
    n = len(close_arr)

    fu = np.full(n, np.nan)
    fl = np.full(n, np.nan)
    dir_arr = np.zeros(n)
    line_arr = np.full(n, np.nan)

    for i in range(n):
        if np.isnan(upper[i]) or np.isnan(close_arr[i]):
            dir_arr[i] = dir_arr[i - 1] if i else 1.0
            continue
        if i == 0:
            fu[i], fl[i] = upper[i], lower[i]
            dir_arr[i] = 1.0 if close_arr[i] >= hl2.iloc[i] else -1.0
            line_arr[i] = fl[i] if dir_arr[i] > 0 else fu[i]
            continue
        prev_fu = fu[i - 1] if not np.isnan(fu[i - 1]) else upper[i]
        prev_fl = fl[i - 1] if not np.isnan(fl[i - 1]) else lower[i]
        # Верхняя полоса может только опускаться (или сбрасываться при пробое).
        fu[i] = upper[i] if (upper[i] < prev_fu or close_arr[i - 1] > prev_fu) else prev_fu
        # Нижняя полоса может только подниматься (или сбрасываться при пробое).
        fl[i] = lower[i] if (lower[i] > prev_fl or close_arr[i - 1] < prev_fl) else prev_fl

        if close_arr[i] > prev_fu:
            dir_arr[i] = 1.0
        elif close_arr[i] < prev_fl:
            dir_arr[i] = -1.0
        else:
            dir_arr[i] = dir_arr[i - 1]
        line_arr[i] = fl[i] if dir_arr[i] > 0 else fu[i]

    return (pd.Series(line_arr, index=close.index),
            pd.Series(dir_arr, index=close.index))


# ---------------------------------------------------------------------------
# Объём
# ---------------------------------------------------------------------------

def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    sign = np.sign(close.diff().fillna(0.0))
    return (sign * volume).cumsum()


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
        period: int = 14) -> pd.Series:
    """Money Flow Index — «RSI с объёмом»."""
    tp = typical_price(high, low, close)
    raw_flow = tp * volume
    delta = tp.diff()
    pos = pd.Series(np.where(delta > 0, raw_flow, 0.0), index=close.index)
    neg = pd.Series(np.where(delta < 0, raw_flow, 0.0), index=close.index)
    pos_sum = pos.rolling(period, min_periods=period).sum()
    neg_sum = neg.rolling(period, min_periods=period).sum()
    ratio = pos_sum / neg_sum.replace(0, np.nan)
    out = 100 - 100 / (1 + ratio)
    return out.where(neg_sum != 0, 100.0).where(pos_sum.notna())


def vwap(high: pd.Series, low: pd.Series, close: pd.Series,
         volume: pd.Series, period: Optional[int] = None) -> pd.Series:
    """
    VWAP. Без ``period`` — накопительный от начала ряда (сессия),
    с ``period`` — скользящий (удобно для крипто 24/7).
    """
    tp = typical_price(high, low, close)
    pv = tp * volume
    if period is None:
        cum_vol = volume.cumsum().replace(0, np.nan)
        return pv.cumsum() / cum_vol
    vol_sum = volume.rolling(period, min_periods=period).sum().replace(0, np.nan)
    return pv.rolling(period, min_periods=period).sum() / vol_sum


def typical_price(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    return (high + low + close) / 3


# ---------------------------------------------------------------------------
# Статистика и уровни
# ---------------------------------------------------------------------------

def zscore(series: pd.Series, period: int = 20) -> pd.Series:
    """Сколько сигм от своей скользящей средней — основа аномалий."""
    period = int(period)
    mean = series.rolling(period, min_periods=period).mean()
    std = series.rolling(period, min_periods=period).std(ddof=0)
    return (series - mean) / std.replace(0, np.nan)


def percentile_rank(series: pd.Series, period: int = 100) -> pd.Series:
    """
    Перцентиль текущего значения внутри своего окна, 0..100.

    Используется для «сжатия волатильности»: BB-width на 10-м перцентиле
    за 100 баров — это и есть сжатая пружина.
    """
    period = int(period)

    def _rank(window: np.ndarray) -> float:
        if np.isnan(window).any():
            return np.nan
        last = window[-1]
        return float((window < last).sum()) / (len(window) - 1) * 100

    return series.rolling(period, min_periods=max(10, period // 2)).apply(_rank, raw=True)


def donchian(high: pd.Series, low: pd.Series, period: int = 20
             ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    upper = high.rolling(period, min_periods=period).max()
    lower = low.rolling(period, min_periods=period).min()
    return upper, lower, (upper + lower) / 2


def highest(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(int(period), min_periods=int(period)).max()


def lowest(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(int(period), min_periods=int(period)).min()


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    """True в баре, где ``a`` пересекла ``b`` снизу вверх."""
    prev = (a.shift(1) <= b.shift(1))
    now = a > b
    return (prev & now).fillna(False)


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    prev = (a.shift(1) >= b.shift(1))
    now = a < b
    return (prev & now).fillna(False)


def linear_slope(series: pd.Series, period: int = 20,
                 normalize: bool = True) -> pd.Series:
    """
    Наклон линейной регрессии (в % за бар, если normalize).
    Даёт «силу и направление» тренда без лага EMA.
    """
    period = int(period)

    def _slope(window: np.ndarray) -> float:
        if np.isnan(window).any():
            return np.nan
        x = np.arange(len(window), dtype="float64")
        slope = np.polyfit(x, window, 1)[0]
        if normalize:
            base = window.mean()
            return float(slope / base * 100) if base else float("nan")
        return float(slope)

    return series.rolling(period, min_periods=period).apply(_slope, raw=True)


def r_squared(series: pd.Series, period: int = 20) -> pd.Series:
    """Качество линейного тренда: 1 = идеальная прямая, 0 = шум."""
    period = int(period)

    def _r2(window: np.ndarray) -> float:
        if np.isnan(window).any():
            return np.nan
        x = np.arange(len(window), dtype="float64")
        coef = np.polyfit(x, window, 1)
        pred = np.polyval(coef, x)
        ss_res = float(np.sum((window - pred) ** 2))
        ss_tot = float(np.sum((window - window.mean()) ** 2))
        return 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot

    return series.rolling(period, min_periods=period).apply(_r2, raw=True)


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def safe_float(value, default: float = float("nan")) -> float:
    """Безопасное преобразование в float (NaN/None/мусор → default)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def last_valid(series: pd.Series, default: float = float("nan")) -> float:
    """Последнее не-NaN значение (или default)."""
    if series is None or len(series) == 0:
        return default
    valid = series.dropna()
    return float(valid.iloc[-1]) if len(valid) else default
