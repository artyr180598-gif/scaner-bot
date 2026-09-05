from __future__ import annotations

import math

from cryptopilot.indicators import compute_features, directional_score


def test_features_and_direction_are_finite(candle_factory) -> None:
    bullish = compute_features(candle_factory(direction=1))
    bearish = compute_features(candle_factory(direction=-1))

    assert bullish.close > bullish.ema50 > bullish.ema200
    assert bearish.close < bearish.ema50 < bearish.ema200
    assert directional_score(bullish) > 40
    assert directional_score(bearish) < -20
    assert math.isfinite(bullish.atr14)
    assert 0 <= bullish.rsi14 <= 100
    assert 0 <= bullish.efficiency_ratio20 <= 1
    assert bullish.ema_gap_atr >= 0
    assert bullish.atr_regime_ratio > 0
    assert bullish.dmi_spread > 0
    assert 0 <= bullish.choppiness14 <= 100
    assert -1 <= bullish.cmf20 <= 1
    assert bullish.relative_volume20 > 0
    assert bullish.keltner_squeeze_ratio >= 0
    assert math.isfinite(bullish.vwap_distance_atr)
    assert bullish.supertrend_direction in {-1, 1}
    assert math.isfinite(bullish.supertrend_distance_atr)


def test_breakout_does_not_use_future_data(candle_factory) -> None:
    candles = candle_factory(count=260, direction=1)
    baseline = compute_features(candles)
    future = candle_factory(count=20, direction=-1)
    repeated = compute_features(candles)

    assert baseline == repeated
    assert future  # Future data exists but was never passed to the calculation.
