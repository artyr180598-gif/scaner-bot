from cryptopilot.models import Candle
from scripts.signal_quality_study import simulate


def test_stop_first_and_gap_loss_not_clipped():
    bars = [Candle(0, 100, 100.5, 99.5, 100, 1), Candle(900000, 95, 103, 94, 100, 1)]
    result = simulate(bars, 0, 1, 99, horizon=2)
    assert result["exit"] == 95 and result["gross_r"] == -5
    bars[1] = Candle(900000, 100, 103, 98, 100, 1)
    assert simulate(bars, 0, 1, 99, horizon=2)["reason"] == "SL"


def test_cost_stress_and_short_symmetry():
    bars = [Candle(0, 100, 102.5, 99.5, 102, 1)]
    result = simulate(bars, 0, 1, 99, horizon=1)
    assert result["gross_r"] == 2 and result["stress_r"] < result["net_r"] < 2
    short = [Candle(0, 100, 100.5, 97.5, 98, 1)]
    assert simulate(short, 0, -1, 101, horizon=1)["gross_r"] == 2


def test_incomplete_horizon_is_not_scored():
    assert simulate([Candle(0, 100, 100, 100, 100, 1)], 0, 1, 99, horizon=2) is None
