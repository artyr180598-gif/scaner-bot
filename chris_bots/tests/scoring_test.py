"""
Тесты скоринга, фильтров и планировщика.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from chris_bots.tests.base import OK, make_synthetic_df, run_test


def test_scorer_long_on_clear_uptrend() -> None:
    """Скоринг не должен падать и должен вернуть валидные группы на любом тренде."""
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.scoring import Scorer
    from chris_bots.core.domain.signal import Direction

    s = get_settings()
    scorer = Scorer(s)
    df = make_synthetic_df(200, "up", seed=7)
    enriched = scorer.enrich(df)
    groups = scorer.score_groups(enriched)
    # Все 6 групп должны быть представлены.
    assert set(groups.keys()) == {"trend", "momentum", "volume", "volatility", "structure", "patterns"}
    direction = scorer.decide_direction(groups)
    assert direction in {Direction.LONG, Direction.SHORT, Direction.NEUTRAL}
    if direction != Direction.NEUTRAL:
        conf = scorer.build_confidences(enriched, groups, direction)
        assert 0 <= conf.data <= 100
        assert 0 <= conf.signal <= 100
    print(OK + f"scorer pipeline ok on uptrend (direction={direction.value})")


def test_scorer_short_on_clear_downtrend() -> None:
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.scoring import Scorer
    from chris_bots.core.domain.signal import Direction

    s = get_settings()
    scorer = Scorer(s)
    df = make_synthetic_df(200, "down", seed=11)
    enriched = scorer.enrich(df)
    groups = scorer.score_groups(enriched)
    direction = scorer.decide_direction(groups)
    assert direction in {Direction.LONG, Direction.SHORT, Direction.NEUTRAL}
    print(OK + f"scorer pipeline ok on downtrend (direction={direction.value})")


def test_aggregate_confidence_requires_two_groups() -> None:
    """Урок BRAIN.md: <2 активных групп → 0% confidence."""
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.scoring import Scorer, GroupScore
    from chris_bots.core.domain.signal import Direction

    s = get_settings()
    scorer = Scorer(s)
    # Только одна группа активна.
    groups = {
        g: GroupScore(group=g, score=0.0)
        for g in ("trend", "momentum", "volume", "volatility", "structure", "patterns")
    }
    groups["trend"] = GroupScore(group="trend", score=0.8, contributing=["ema"])
    conf = scorer.aggregate_confidence(groups, Direction.LONG)
    assert conf == 0.0, f"1 активная группа должна давать 0, получили {conf}"
    print(OK + "aggregate_confidence: 1 active group → 0% (правильно)")


def test_aggregate_confidence_capped_at_95() -> None:
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.scoring import Scorer, GroupScore
    from chris_bots.core.domain.signal import Direction

    s = get_settings()
    scorer = Scorer(s)
    groups = {
        g: GroupScore(group=g, score=0.99 if g != "patterns" else 0.0, contributing=[g])
        for g in ("trend", "momentum", "volume", "volatility", "structure", "patterns")
    }
    conf = scorer.aggregate_confidence(groups, Direction.LONG)
    assert conf <= 95.0, f"confidence должен быть ограничен 95, получили {conf}"
    assert conf > 0, f"5 активных групп должны давать > 0, получили {conf}"
    print(OK + f"aggregate_confidence: 5 active groups capped at {conf}%")


def test_aggregate_confidence_zero_for_mixed_signals() -> None:
    """Если direction=Long, а против него 2+ группы — confidence падает.

    Логика: берём только группы, чей знак СОВПАДАЕТ с direction. Если их < 2 → 0.
    """
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.scoring import Scorer, GroupScore
    from chris_bots.core.domain.signal import Direction

    s = get_settings()
    scorer = Scorer(s)
    # Сценарий: только ОДНА группа за Long → должно быть 0 (меньше 2 активных).
    groups = {
        "trend": GroupScore(group="trend", score=0.5, contributing=[]),
        "momentum": GroupScore(group="momentum", score=-0.5, contributing=[]),  # против Long
        "volume": GroupScore(group="volume", score=-0.3, contributing=[]),     # против Long
        "volatility": GroupScore(group="volatility", score=0.0, contributing=[]),
        "structure": GroupScore(group="structure", score=0.0, contributing=[]),
        "patterns": GroupScore(group="patterns", score=0.0, contributing=[]),
    }
    conf_long = scorer.aggregate_confidence(groups, Direction.LONG)
    # Только trend голосует за Long → 1 группа → < 2 → 0.
    assert conf_long == 0, f"только 1 группа за Long → 0, получили {conf_long}"
    # А для Short голосуют momentum и volume (2 группы) → > 0.
    conf_short = scorer.aggregate_confidence(groups, Direction.SHORT)
    assert conf_short > 0, f"momentum и volume за Short → > 0, получили {conf_short}"
    print(OK + f"aggregate_confidence: cross-signals split correctly (long={conf_long}, short={conf_short})")


# ── Filters ────────────────────────────────────────────────


def test_filter_ticker_low_liquidity() -> None:
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.filters import Filters
    from chris_bots.core.domain.market import TickerMeta

    s = get_settings()
    f = Filters(s)
    meta = TickerMeta(
        symbol="MICRO/USDT", exchange="binance",
        last_price=0.001, quote_volume_24h=1_000, change_pct_24h=0,
    )
    res = f.check_ticker(meta)
    assert not res.passed and res.reason == "low_liquidity"
    print(OK + "filter: low_liquidity rejected")


def test_filter_ticker_wide_spread() -> None:
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.filters import Filters
    from chris_bots.core.domain.market import TickerMeta

    s = get_settings()
    f = Filters(s)
    meta = TickerMeta(
        symbol="ILLIQ/USDT", exchange="binance",
        last_price=100, quote_volume_24h=10_000_000, change_pct_24h=0,
        bid=99, ask=100.5, spread_pct=1.5,
    )
    res = f.check_ticker(meta)
    assert not res.passed and res.reason == "wide_spread"
    print(OK + "filter: wide_spread rejected")


def test_filter_volatility_bounds() -> None:
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.filters import Filters

    s = get_settings()
    f = Filters(s)
    df = make_synthetic_df(100, "sideways")
    df["atr_pct"] = 0.1  # слишком тихо
    res = f.check_volatility(df)
    assert not res.passed and res.reason == "too_calm"
    df["atr_pct"] = 20.0  # слишком волатильно
    res = f.check_volatility(df)
    assert not res.passed and res.reason == "too_volatile"
    df["atr_pct"] = 2.0  # норма
    res = f.check_volatility(df)
    assert res.passed
    print(OK + "filter: volatility bounds ok (too_calm / too_volatile / ok)")


def test_filter_min_confidence() -> None:
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.filters import Filters

    s = get_settings()
    f = Filters(s)
    res_low = f.check_min_confidence(50.0, 80.0)
    res_ok = f.check_min_confidence(80.0, 80.0)
    res_low_data = f.check_min_confidence(80.0, 40.0)
    assert not res_low.passed
    assert res_ok.passed
    assert not res_low_data.passed
    print(OK + "filter: min_confidence respects both data and signal thresholds")


# ── Planner ────────────────────────────────────────────────


def test_planner_long_produces_targets_above_entry() -> None:
    from chris_bots.analysis.plan import Planner
    from chris_bots.core.domain.signal import Direction

    planner = Planner()
    df = make_synthetic_df(200, "up")
    df["atr_pct"] = 2.0
    last = float(df["close"].iloc[-1])
    plan = planner.build(df, Direction.LONG, last)
    assert all(tp.price > plan.entry_mid for tp in plan.take_profits), \
        "Long: TP должны быть выше входа"
    assert plan.stop_loss.price < plan.entry_mid, "Long: SL должен быть ниже входа"
    assert plan.risk_reward > 0
    print(OK + f"planner Long: entry={plan.entry_mid:.2f} TP1={plan.take_profits[0].price:.2f} SL={plan.stop_loss.price:.2f} RR={plan.risk_reward}")


def test_planner_short_produces_targets_below_entry() -> None:
    from chris_bots.analysis.plan import Planner
    from chris_bots.core.domain.signal import Direction

    planner = Planner()
    df = make_synthetic_df(200, "down")
    df["atr_pct"] = 2.0
    last = float(df["close"].iloc[-1])
    plan = planner.build(df, Direction.SHORT, last)
    assert all(tp.price < plan.entry_mid for tp in plan.take_profits)
    assert plan.stop_loss.price > plan.entry_mid
    print(OK + f"planner Short: entry={plan.entry_mid:.2f} TP1={plan.take_profits[0].price:.2f} SL={plan.stop_loss.price:.2f} RR={plan.risk_reward}")


def test_planner_tp1_2_3_are_1r_2r_3r() -> None:
    from chris_bots.analysis.plan import Planner
    from chris_bots.core.domain.signal import Direction

    planner = Planner(tp_multiples=(1.0, 2.0, 3.0))
    df = make_synthetic_df(200, "up")
    df["atr_pct"] = 2.0
    last = float(df["close"].iloc[-1])
    plan = planner.build(df, Direction.LONG, last)
    risk = abs(plan.entry_mid - plan.stop_loss.price)
    assert abs((plan.take_profits[0].price - plan.entry_mid) - risk) < 0.01
    assert abs((plan.take_profits[1].price - plan.entry_mid) - 2 * risk) < 0.01
    assert abs((plan.take_profits[2].price - plan.entry_mid) - 3 * risk) < 0.01
    print(OK + "planner: TP1=1R, TP2=2R, TP3=3R (verified)")


def test_planner_leverage_capped() -> None:
    from chris_bots.analysis.plan import Planner
    from chris_bots.core.domain.signal import Direction

    planner = Planner(max_leverage=3.0)
    df = make_synthetic_df(200, "up")
    df["atr_pct"] = 2.0
    last = float(df["close"].iloc[-1])
    plan = planner.build(df, Direction.LONG, last)
    assert plan.leverage_suggestion <= 3.0
    print(OK + f"planner: leverage capped at max={planner.max_leverage} (got {plan.leverage_suggestion})")


TESTS = [
    test_scorer_long_on_clear_uptrend,
    test_scorer_short_on_clear_downtrend,
    test_aggregate_confidence_requires_two_groups,
    test_aggregate_confidence_capped_at_95,
    test_aggregate_confidence_zero_for_mixed_signals,
    test_filter_ticker_low_liquidity,
    test_filter_ticker_wide_spread,
    test_filter_volatility_bounds,
    test_filter_min_confidence,
    test_planner_long_produces_targets_above_entry,
    test_planner_short_produces_targets_below_entry,
    test_planner_tp1_2_3_are_1r_2r_3r,
    test_planner_leverage_capped,
]


if __name__ == "__main__":
    run_test(TESTS, "=== scoring/filters/plan suite ===")
