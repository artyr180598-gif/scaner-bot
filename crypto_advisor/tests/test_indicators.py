"""Быстрая проверка индикаторов и группового скоринга."""

from __future__ import annotations

from crypto_advisor.indicators import ALL_GROUPS, all_indicators, attach_signals
from crypto_advisor.strategy import get_strategy


def test_all_indicators_registered():
    inds = all_indicators()
    assert len(inds) >= 10
    groups = {i.group for i in inds}
    assert groups.issuperset({"trend", "momentum", "volume"})
    assert ALL_GROUPS


def test_attach_signals_returns_columns():
    import pandas as pd
    df = pd.DataFrame({
        "timestamp": range(120),
        "open": [1.0] * 120,
        "high": [1.05] * 120,
        "low": [0.95] * 120,
        "close": [1.0 + i * 0.001 for i in range(120)],
        "volume": [1000 + i for i in range(120)],
    })
    out = attach_signals(df, all_indicators())
    for g in ALL_GROUPS:
        assert f"{g}_signal" in out.columns


def test_strategy_direction_deterministic():
    import pandas as pd
    df = pd.DataFrame({
        "timestamp": range(120),
        "open": [1.0] * 120,
        "high": [1.05] * 120,
        "low": [0.95] * 120,
        "close": [1.0 + i * 0.02 for i in range(120)],
        "volume": [1000.0] * 120,
    })
    strat = get_strategy("momentum_trend")
    df = strat.populate_indicators(df)
    scores = strat.group_scores(df)
    direction = strat.decide_direction(scores)
    # Восходящий тренд — Long (или как минимум не SHORT).
    assert direction.value != "Short"
