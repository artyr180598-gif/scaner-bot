from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cryptopilot.backtest import WalkForwardBacktester
from cryptopilot.models import Candle


def test_gap_stop_and_notional_cost_are_not_clipped():
    candles = [Candle(i * 900000, 100, 100.5, 99.5, 100, 1) for i in range(260)]
    candles[222] = Candle(222 * 900000, 95, 100, 94, 95, 1)
    with (
        patch("cryptopilot.backtest.compute_features", return_value=SimpleNamespace(atr14=1)),
        patch("cryptopilot.backtest.directional_score", side_effect=[50] + [0] * 300),
    ):
        result = WalkForwardBacktester().run("TEST", "15m", candles, one_way_cost_bps=6)
    assert result.trades == 1
    assert result.expectancy_r == pytest.approx((-5 - 195 * 0.0006) / 1.45)


def test_costs_scale_with_notional_and_higher_costs_reduce_expectancy(candle_factory):
    candles = candle_factory(interval="60", count=700, direction=1)
    baseline = WalkForwardBacktester().run("TEST", "1h", candles, one_way_cost_bps=0)
    stress = WalkForwardBacktester().run("TEST", "1h", candles, one_way_cost_bps=12)
    assert baseline.trades == stress.trades
    assert baseline.expectancy_r > stress.expectancy_r


def test_walk_forward_backtest_returns_risk_metrics(candle_factory) -> None:
    candles = candle_factory(interval="60", count=700, direction=1)
    result = WalkForwardBacktester().run("TESTUSDT", "1h", candles)

    assert result.bars == 700
    assert result.trades > 0
    assert result.wins + result.losses == result.trades
    assert 0 <= result.win_rate <= 100
    assert math.isfinite(result.expectancy_r)
    assert result.max_drawdown_r >= 0
