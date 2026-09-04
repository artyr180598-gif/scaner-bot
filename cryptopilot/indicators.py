from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from cryptopilot.models import Candle, FeatureSet


class InsufficientData(ValueError):
    pass


def ema(values: np.ndarray, period: int) -> np.ndarray:
    if len(values) < period:
        raise InsufficientData(f"EMA{period} needs {period} values")
    result = np.empty_like(values, dtype=float)
    result[0] = float(values[0])
    alpha = 2.0 / (period + 1)
    for index in range(1, len(values)):
        result[index] = alpha * values[index] + (1 - alpha) * result[index - 1]
    return result


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    if len(values) <= period:
        raise InsufficientData(f"RSI{period} needs more than {period} values")
    delta = np.diff(values, prepend=values[0])
    gains = np.maximum(delta, 0.0)
    losses = np.maximum(-delta, 0.0)
    avg_gain = ema(gains, period)
    avg_loss = ema(losses, period)
    relative = np.divide(
        avg_gain,
        avg_loss,
        out=np.full_like(avg_gain, np.inf),
        where=avg_loss > 1e-12,
    )
    return 100.0 - (100.0 / (1.0 + relative))


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    previous = np.roll(close, 1)
    previous[0] = close[0]
    return np.maximum.reduce((high - low, np.abs(high - previous), np.abs(low - previous)))


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    return ema(true_range(high, low, close), period)


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    if len(close) <= period * 2:
        raise InsufficientData(f"ADX{period} needs more than {period * 2} values")
    up = np.diff(high, prepend=high[0])
    down = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr_smoothed = ema(true_range(high, low, close), period)
    plus_di = 100 * np.divide(
        ema(plus_dm, period), tr_smoothed, out=np.zeros_like(close), where=tr_smoothed > 1e-12
    )
    minus_di = 100 * np.divide(
        ema(minus_dm, period), tr_smoothed, out=np.zeros_like(close), where=tr_smoothed > 1e-12
    )
    denominator = plus_di + minus_di
    dx = 100 * np.divide(
        np.abs(plus_di - minus_di),
        denominator,
        out=np.zeros_like(close),
        where=denominator > 1e-12,
    )
    return ema(dx, period)


def zscore_last(values: np.ndarray, period: int = 30) -> float:
    window = values[-period:]
    deviation = float(np.std(window))
    return 0.0 if deviation < 1e-12 else float((window[-1] - np.mean(window)) / deviation)


def compute_features(candles: Sequence[Candle]) -> FeatureSet:
    if len(candles) < 210:
        raise InsufficientData("At least 210 closed candles are required")
    close = np.asarray([x.close for x in candles], dtype=float)
    high = np.asarray([x.high for x in candles], dtype=float)
    low = np.asarray([x.low for x in candles], dtype=float)
    volume = np.asarray([x.volume for x in candles], dtype=float)

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    adx14 = adx(high, low, close, 14)
    macd = ema(close, 12) - ema(close, 26)
    macd_hist = macd - ema(macd, 9)

    middle = float(np.mean(close[-20:]))
    band_width = 2 * float(np.std(close[-20:]))
    lower, upper = middle - band_width, middle + band_width
    bb_position = 0.5 if upper == lower else (float(close[-1]) - lower) / (upper - lower)
    current_atr = float(atr14[-1])
    current_close = float(close[-1])

    values = FeatureSet(
        close=current_close,
        ema20=float(ema20[-1]),
        ema50=float(ema50[-1]),
        ema200=float(ema200[-1]),
        ema20_slope_pct=float((ema20[-1] / ema20[-6] - 1) * 100),
        rsi14=float(rsi14[-1]),
        atr14=current_atr,
        atr_pct=(current_atr / current_close * 100) if current_close else 0.0,
        adx14=float(adx14[-1]),
        macd_hist=float(macd_hist[-1]),
        bb_position=float(bb_position),
        volume_z=zscore_last(volume, 30),
        breakout_up=current_close > float(np.max(high[-21:-1])),
        breakout_down=current_close < float(np.min(low[-21:-1])),
        return_20_pct=float((close[-1] / close[-21] - 1) * 100),
    )
    numeric = (
        values.close,
        values.ema20,
        values.ema50,
        values.ema200,
        values.ema20_slope_pct,
        values.rsi14,
        values.atr14,
        values.atr_pct,
        values.adx14,
        values.macd_hist,
        values.bb_position,
        values.volume_z,
        values.return_20_pct,
    )
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("Indicator calculation produced a non-finite value")
    return values


def directional_score(features: FeatureSet) -> float:
    """Core score in [-100, 100], positive for long and negative for short."""
    score = 0.0
    if features.close > features.ema20 > features.ema50 > features.ema200:
        score += 34
    elif features.close < features.ema20 < features.ema50 < features.ema200:
        score -= 34
    else:
        score += 10 if features.close > features.ema50 else -10

    score += float(np.clip(features.ema20_slope_pct * 7, -14, 14))
    if 52 <= features.rsi14 <= 68:
        score += 12
    elif 32 <= features.rsi14 <= 48:
        score -= 12
    elif features.rsi14 > 78:
        score -= 8
    elif features.rsi14 < 22:
        score += 8

    score += 10 if features.macd_hist > 0 else -10
    score += 10 if features.breakout_up else (-10 if features.breakout_down else 0)
    if features.volume_z > 1:
        score += 8 if features.return_20_pct > 0 else -8
    if features.adx14 < 16:
        score *= 0.72
    return float(np.clip(score, -100, 100))
