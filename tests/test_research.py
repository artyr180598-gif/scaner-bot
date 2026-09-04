from __future__ import annotations

import pytest

from cryptopilot.indicators import compute_features, directional_score
from cryptopilot.models import Candle
from cryptopilot.research import aggregate_candles, feature_arrays, rolling_zscore


def test_feature_series_matches_live_score_at_last_bar(candle_factory) -> None:
    candles = candle_factory(interval="15", count=400, direction=1)
    series = feature_arrays(candles)
    live = directional_score(compute_features(candles))

    assert series.score[-1] == pytest.approx(live)


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
