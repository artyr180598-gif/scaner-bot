from types import SimpleNamespace

from scripts.coin_selection_study import HistoricalSelector, rank_coins


def test_ranking_mirrors_strength_for_short():
    snapshot = {"BTCUSDT": (0, 1, 1), "A": (3, 1, 1), "B": (-3, 1, 1), "C": (1, 1, 1)}
    assert rank_coins(snapshot, 1, 1) == ["A"]
    assert rank_coins(snapshot, -1, 1) == ["B"]


def test_missing_data_and_insufficient_coverage_do_not_rank():
    assert rank_coins({"BTCUSDT": (0, 1, 1)}, 1) == []
    snapshot = {"BTCUSDT": (0, 1, 1), "A": (3, 1, 1), "B": (5, 1, float("nan")), "C": (1, 1, 1)}
    assert rank_coins(snapshot, 1) == []


def test_extreme_volume_excluded_and_ties_deterministic():
    snapshot = {s: (0, 1, 1) for s in ("BTCUSDT", "A", "B", "C")}
    snapshot["PUMP"] = (100, 0.1, 5)
    assert rank_coins(snapshot, 1) == ["A", "B"]
    assert rank_coins(dict(reversed(list(snapshot.items()))), 1) == ["A", "B"]


def test_historical_ranking_does_not_read_future_bar():
    selector = HistoricalSelector.__new__(HistoricalSelector)
    selector.audit = {}
    selector.data = {}
    for symbol in ("BTCUSDT", "A", "B", "C"):
        selector.data[symbol] = (
            list(range(0, 222 * 900000, 900000)),
            SimpleNamespace(
                return_20_pct=[0.0] * 222,
                atr_regime_ratio=[1.0] * 222,
                relative_volume20=[1.0] * 222,
            ),
        )
    before = selector.selected(220 * 900000, 1)
    selector.data["C"][1].return_20_pct[221] = 1000
    selector.audit = {}
    assert selector.selected(220 * 900000, 1) == before
    assert selector.selected(220 * 900000 - 1, 1) == []  # not the required closed-bar timestamp
