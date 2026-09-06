from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from cryptopilot.config import Settings
from cryptopilot.indicators import compute_features
from cryptopilot.models import Side
from cryptopilot.prime_plan import build_prime_plan


def test_prime_plan_long_is_before_trigger_and_has_positive_rr(candle_factory) -> None:
    candles = candle_factory(direction=1)
    feature = compute_features(candles)
    feature = replace(
        feature,
        range_high20=feature.close + feature.atr14 * 0.8,
        range_low20=feature.close - feature.atr14 * 2.0,
    )
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
        prime_max_stop_pct=5.0,
    )

    result = build_prime_plan(
        Side.LONG,
        feature.close,
        feature.range_high20,
        candle_factory(interval="5", direction=1),
        feature,
        settings,
        now=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    assert result.plan is not None
    plan = result.plan
    assert plan.entry_low < plan.entry_high < feature.range_high20
    assert plan.stop_loss < plan.entry_low
    assert plan.take_profit_1 > plan.entry_high
    assert plan.take_profit_2 > plan.take_profit_1
    assert plan.take_profit_3 > plan.take_profit_2
    assert plan.risk_reward_2 >= settings.prime_min_plan_rr
    assert plan.scale_allocations_pct == (100, 0, 0)


def test_prime_plan_short_is_before_trigger_and_has_stop_above(candle_factory) -> None:
    candles = candle_factory(direction=-1)
    feature = compute_features(candles)
    feature = replace(
        feature,
        range_low20=feature.close - feature.atr14 * 0.8,
        range_high20=feature.close + feature.atr14 * 2.0,
    )
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
        prime_max_stop_pct=5.0,
    )

    result = build_prime_plan(
        Side.SHORT,
        feature.close,
        feature.range_low20,
        candle_factory(interval="5", direction=-1),
        feature,
        settings,
    )

    assert result.plan is not None
    plan = result.plan
    assert feature.range_low20 < plan.entry_low < plan.entry_high
    assert plan.stop_loss > plan.entry_high
    assert plan.take_profit_1 < plan.entry_low
    assert plan.take_profit_2 < plan.take_profit_1
    assert plan.take_profit_3 < plan.take_profit_2


def test_prime_plan_rejects_late_entry_when_trigger_too_close(candle_factory) -> None:
    candles = candle_factory(direction=1)
    feature = compute_features(candles)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
    )
    trigger = feature.close + max(feature.atr14 * 0.01, feature.close * 0.00001)

    result = build_prime_plan(
        Side.LONG,
        feature.close,
        trigger,
        candle_factory(interval="5", direction=1),
        feature,
        settings,
    )

    assert result.plan is None
    assert result.blockers


@pytest.mark.parametrize("side,direction", [(Side.LONG, 1), (Side.SHORT, -1)])
def test_zone_worst_fill_respects_risk_with_costs(candle_factory, side, direction):
    feature = compute_features(candle_factory(direction=direction))
    settings = Settings(_env_file=None, prime_max_stop_pct=5.0)
    result = build_prime_plan(
        side,
        feature.close,
        feature.close + direction * feature.atr14 * 0.8,
        candle_factory(interval="5", direction=direction),
        feature,
        settings,
    )
    assert result.plan is not None
    plan = result.plan
    budget = settings.account_equity_usdt * settings.risk_per_trade_pct / 100
    budget *= settings.prime_risk_multiplier
    for entry in (plan.entry_low, plan.entry_high):
        loss = abs(entry - plan.stop_loss)
        loss += (entry + plan.stop_loss) * settings.paper_one_way_cost_bps / 10_000
        assert plan.suggested_quantity * loss <= budget + 1e-9
        assert plan.suggested_quantity * loss <= plan.risk_amount + 1e-9


@pytest.mark.parametrize("side,direction", [(Side.LONG, 1), (Side.SHORT, -1)])
def test_crossed_trigger_is_not_early_entry(candle_factory, side, direction):
    feature = compute_features(candle_factory(direction=direction))
    result = build_prime_plan(
        side,
        feature.close,
        feature.close * (1 - direction * 0.005),
        candle_factory(interval="5", direction=direction),
        feature,
        Settings(_env_file=None, prime_max_stop_pct=5.0),
    )
    assert result.plan is None
    assert "trigger" in result.blockers[0]
