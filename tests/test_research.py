from __future__ import annotations

import pytest

from cryptopilot.indicators import compute_features, directional_score
from cryptopilot.models import Candle
from cryptopilot.research import (
    aggregate_candles,
    feature_arrays,
    rolling_efficiency_ratio,
    rolling_zscore,
)


def test_feature_series_matches_live_score_at_last_bar(candle_factory) -> None:
    candles = candle_factory(interval="15", count=400, direction=1)
    series = feature_arrays(candles)
    live_features = compute_features(candles)
    live = directional_score(live_features)

    assert series.score[-1] == pytest.approx(live)
    assert series.efficiency_ratio20[-1] == pytest.approx(live_features.efficiency_ratio20)
    assert series.ema_gap_atr[-1] == pytest.approx(live_features.ema_gap_atr)
    assert series.atr_regime_ratio[-1] == pytest.approx(live_features.atr_regime_ratio)
    assert series.dmi_spread[-1] == pytest.approx(live_features.dmi_spread)
    assert series.bb_width_pct[-1] == pytest.approx(live_features.bb_width_pct)
    assert series.bb_width_regime_ratio[-1] == pytest.approx(
        live_features.bb_width_regime_ratio
    )
    assert series.range_position20[-1] == pytest.approx(live_features.range_position20)
    assert series.keltner_squeeze_ratio[-1] == pytest.approx(
        live_features.keltner_squeeze_ratio
    )
    assert series.squeeze_bars[-1] == live_features.squeeze_bars
    assert series.choppiness14[-1] == pytest.approx(live_features.choppiness14)
    assert series.cmf20[-1] == pytest.approx(live_features.cmf20)
    assert series.relative_volume20[-1] == pytest.approx(live_features.relative_volume20)
    assert series.vwap_distance_atr[-1] == pytest.approx(live_features.vwap_distance_atr)
    assert series.supertrend_direction[-1] == live_features.supertrend_direction
    assert series.supertrend_distance_atr[-1] == pytest.approx(
        live_features.supertrend_distance_atr
    )


def test_aggregation_ignores_incomplete_higher_timeframe(candle_factory) -> None:
    candles = candle_factory(interval="15", count=9, direction=1)
    # Force alignment so two complete groups plus one incomplete candle are deterministic.
    first = candles[0].open_time_ms // 3_600_000 * 3_600_000
    aligned = [
        Candle(
            open_time_ms=first + index * 900_000,
            open=item.open,
            high=item.high,
            low=item.low,
            close=item.close,
            volume=item.volume,
            turnover=item.turnover,
        )
        for index, item in enumerate(candles)
    ]

    hourly = aggregate_candles(aligned, 60)

    assert len(hourly) == 2
    assert hourly[0].open == aligned[0].open
    assert hourly[0].close == aligned[3].close
    assert hourly[1].close == aligned[7].close


def test_rolling_zscore_uses_only_trailing_window() -> None:
    import numpy as np

    values = np.asarray([1.0, 2.0, 3.0, 100.0])
    actual = rolling_zscore(values, 3)

    assert actual[2] == pytest.approx((3 - 2) / (2 / 3) ** 0.5)
    assert actual[3] > 1


def test_efficiency_ratio_distinguishes_trend_from_noise() -> None:
    import numpy as np

    trend = rolling_efficiency_ratio(np.arange(30, dtype=float), 20)
    noise = rolling_efficiency_ratio(
        np.asarray([0, 1] * 15, dtype=float), 20
    )

    assert trend[-1] == pytest.approx(1)
    assert noise[-1] < 0.1
