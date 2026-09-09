import pytest

from cryptopilot.models import Candle
from scripts.rid_averaging_study import simulate
from scripts.rid_selection_study import selection


def test_selection_needs_aligned_history_and_both_factors():
    panel = {"BTCUSDT": {1: (0.01, 2)}, "ALT": {1: (0.02, 2)}}
    assert selection(panel, "ALT", 1, "combined")
    assert not selection(panel, "BTCUSDT", 1, "relative_strength")
    assert not selection(panel, "ALT", 2, "baseline")
    panel["ALT"][1] = (0.02, 1)
    assert not selection(panel, "ALT", 1, "combined")


def candles():
    return [Candle(i * 900000, 100, 101, 99, 100, 100) for i in range(96)]


def test_all_averaging_policies_have_same_normalized_stop_budget():
    for adds in (0, 1, 2):
        bars = candles()
        bars[0] = Candle(0, 100, 104, 90, 92, 100)
        result = simulate(100, bars, adds)
        assert result["outcome"] == "SL"
        assert result["net_r"] == pytest.approx(-1)
        assert result["adds"] == adds


def test_gap_can_exceed_planned_budget():
    bars = candles()
    bars[0] = Candle(0, 85, 86, 84, 85, 100)
    assert simulate(100, bars, 2)["net_r"] < -1


def test_ambiguous_add_and_target_not_counted_as_instant_win():
    bars = candles()
    bars[0] = Candle(0, 100, 105, 96, 99, 100)
    bars[1] = Candle(900000, 99, 100, 90, 91, 100)
    assert simulate(100, bars, 1)["outcome"] == "SL"


def test_selection_metrics_do_not_change_when_future_bars_are_appended():
    from scripts.rid_selection_study import metrics

    bars = [Candle(i * 900000, 100 + i, 101 + i, 99 + i, 100 + i, 100, 10000) for i in range(130)]
    before = metrics(bars[:110])
    after = metrics(bars)
    assert before
    assert all(after[t] == value for t, value in before.items())


def test_averaging_rejects_unbounded_adds():
    with pytest.raises(ValueError):
        simulate(100, candles(), 3)
