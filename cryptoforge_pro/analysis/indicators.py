"""Deterministic technical indicators.

All indicators are computed from the latest real session candles only. No
forward-looking data is used.
"""

from __future__ import annotations

from typing import Optional

from cryptoforge_pro.models import Candle


def sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> Optional[float]:
    if not values:
        return None
    alpha = 2 / (period + 1)
    out = values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    if losses == 0:
        return 100.0
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        h = candles[i].high
        l = candles[i].low
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    if not trs:
        return None
    return sum(trs[-period:]) / period


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[float]:
    """Return (macd - signal) histogram value."""
    if len(closes) < slow + signal:
        return None
    alpha_fast = 2 / (fast + 1)
    alpha_slow = 2 / (slow + 1)
    alpha_sig = 2 / (signal + 1)
    ema_fast = closes[0]
    ema_slow = closes[0]
    ema_sig = 0.0
    macd_hist = 0.0
    for i, price in enumerate(closes):
        ema_fast = alpha_fast * price + (1 - alpha_fast) * ema_fast
        ema_slow = alpha_slow * price + (1 - alpha_slow) * ema_slow
        macd_val = ema_fast - ema_slow
        if i >= slow:
            ema_sig = alpha_sig * macd_val + (1 - alpha_sig) * ema_sig
            macd_hist = macd_val - ema_sig
    return macd_hist


def roc(closes: list[float], period: int = 10) -> Optional[float]:
    if len(closes) <= period:
        return None
    prev = closes[-period - 1]
    if prev == 0:
        return None
    return (closes[-1] - prev) / prev * 100.0


def volume_ratio(candles: list[Candle], period: int = 20) -> Optional[float]:
    if len(candles) <= period:
        return None
    base = candles[-period - 1 : -1]
    avg_vol = sum(c.volume for c in base) / len(base)
    if avg_vol == 0:
        return None
    return candles[-1].volume / avg_vol


def slope_pct(candles: list[Candle], period: int = 10, use_close: bool = True) -> Optional[float]:
    if len(candles) <= period:
        return None
    first = candles[-period - 1].close if use_close else candles[-period - 1].open
    last = candles[-1].close if use_close else candles[-1].open
    if first == 0:
        return None
    return (last - first) / first * 100.0


def support_resistance(candles: list[Candle], lookback: int = 60) -> tuple[float, float]:
    window = candles[-lookback:] if len(candles) > lookback else candles
    lows = [c.low for c in window]
    highs = [c.high for c in window]
    support = min(lows)
    resistance = max(highs)
    return support, resistance


def swing_high_low(candles: list[Candle], lookback: int = 20) -> tuple[float, float]:
    return max(c.high for c in candles[-lookback:]), min(c.low for c in candles[-lookback:])


def volatility_pct(candles: list[Candle]) -> float:
    if not candles:
        return 0.0
    atr_value = atr(candles, 14)
    if atr_value is None or candles[-1].close == 0:
        return 0.0
    return atr_value / candles[-1].close * 100.0


def breaks_structure(candles: list[Candle], direction: str) -> bool:
    """True when price broke the last meaningful swing in the signal direction."""
    if len(candles) < 12:
        return False
    swing_high, swing_low = swing_high_low(candles, 20)
    if direction == "LONG":
        return candles[-1].close > swing_high
    return candles[-1].close < swing_low


def recent_reversal_strength(candles: list[Candle], direction: str) -> float:
    """0..1; higher when the current candle points at the signal direction."""
    if len(candles) < 3:
        return 0.5
    last = candles[-1]
    prev = candles[-2]
    if last.close <= 0 or prev.close <= 0:
        return 0.5
    last_pct = (last.close - last.open) / last.open * 100.0
    body_pct = abs(last.close - prev.close) / prev.close * 100.0
    if direction == "LONG":
        return max(0.0, min(1.0, last_pct / 2.0 + body_pct / 4.0))
    return max(0.0, min(1.0, -last_pct / 2.0 + body_pct / 4.0))


def last_candle_direction(candles: list[Candle]) -> str:
    if len(candles) < 2 or candles[-1].close == candles[-1].open:
        return "NEUTRAL"
    if candles[-1].close > candles[-1].open:
        return "BULLISH"
    return "BEARISH"
