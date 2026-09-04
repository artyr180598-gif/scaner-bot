from __future__ import annotations

import math

from cryptopilot.backtest import WalkForwardBacktester


def test_walk_forward_backtest_returns_risk_metrics(candle_factory) -> None:
    candles = candle_factory(interval="60", count=700, direction=1)
    result = WalkForwardBacktester().run("TESTUSDT", "1h", candles)

    assert result.bars == 700
    assert result.trades > 0
    assert result.wins + result.losses == result.trades
    assert 0 <= result.win_rate <= 100
    assert math.isfinite(result.expectancy_r)
    assert result.max_drawdown_r >= 0
