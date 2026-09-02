"""
tests/test_planner.py — построение плана сделки.

Проверяем то, за что пользователь платит деньгами:
  * вход зоной, а не «по рынку»;
  * стоп за структурой и в разумных пределах ATR;
  * три цели в правильном порядке и R:R, который совпадает с арифметикой;
  * объём позиции считается от стопа, а плечо не выше пользовательского потолка;
  * лонг и шорт симметричны.
"""

from __future__ import annotations

import pytest

from app.analysis.features import build_features
from app.data.synthetic import make_snapshot
from app.domain.models import Direction, TradePlan
from app.signals.planner import PlanConfig, build_plan, plan_from_config


@pytest.fixture
def cfg() -> PlanConfig:
    return PlanConfig(stop_atr_mult=1.5, tp_atr_mults=(1.5, 2.5, 4.0),
                      risk_pct=1.0, max_leverage=3.0)


@pytest.fixture
def features():
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    return build_features(snapshot, signal_tf=features_tf())


def features_tf():
    from app.domain.models import Timeframe

    return Timeframe.H1


def _plan(direction: Direction, cfg: PlanConfig) -> TradePlan:
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    feats = build_features(snapshot, signal_tf=features_tf())
    plan = build_plan(feats, direction, cfg)
    assert plan is not None, "план должен строиться на валидных данных"
    return plan


# ---------------------------------------------------------------------------
# Валидность
# ---------------------------------------------------------------------------

def test_long_plan_is_valid():
    plan = _plan(Direction.LONG, PlanConfig())
    assert plan.is_valid()
    assert plan.entry_low <= plan.entry_high
    assert plan.stop < plan.entry_low
    prices = [t.price for t in plan.targets]
    assert prices == sorted(prices)
    assert all(t.price > plan.entry_high for t in plan.targets)


def test_short_plan_is_valid():
    plan = _plan(Direction.SHORT, PlanConfig())
    assert plan.is_valid()
    assert plan.entry_low <= plan.entry_high
    assert plan.stop > plan.entry_high
    prices = [t.price for t in plan.targets]
    assert prices == sorted(prices, reverse=True)
    assert all(t.price < plan.entry_low for t in plan.targets)


def test_wait_direction_has_no_plan():
    snapshot = make_snapshot("TEST/USDT", "range", seed=5, bars=300)
    feats = build_features(snapshot, signal_tf=features_tf())
    assert build_plan(feats, Direction.WAIT, PlanConfig()) is None


def test_three_targets_always_present():
    for direction in (Direction.LONG, Direction.SHORT):
        plan = _plan(direction, PlanConfig())
        assert len(plan.targets) == 3
        assert [t.label for t in plan.targets] == ["TP1", "TP2", "TP3"]
        assert sum(t.fraction for t in plan.targets) == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Арифметика R:R и процентов
# ---------------------------------------------------------------------------

def test_rr_and_percent_math():
    plan = _plan(Direction.LONG, PlanConfig())
    mid = (plan.entry_low + plan.entry_high) / 2
    risk = mid - plan.stop
    expected_rr = (plan.targets[0].price - mid) / risk
    assert plan.rr_primary == pytest.approx(expected_rr, rel=1e-9)
    assert plan.target_pcts[0] == pytest.approx(
        (plan.targets[0].price / mid - 1) * 100, rel=1e-9)
    assert plan.stop_pct == pytest.approx((plan.stop / mid - 1) * 100, rel=1e-9)
    assert plan.targets[0].rr == pytest.approx(expected_rr, rel=1e-9)


def test_average_rr_between_primary_and_last():
    plan = _plan(Direction.LONG, PlanConfig())
    assert plan.rr_primary <= plan.rr_avg <= plan.targets[-1].rr + 1e-9


# ---------------------------------------------------------------------------
# Стоп
# ---------------------------------------------------------------------------

def test_stop_within_atr_limits():
    cfg = PlanConfig(stop_atr_mult=1.5, min_stop_atr=0.9, max_stop_atr=4.5)
    plan = _plan(Direction.LONG, cfg)
    dist_atr = abs(plan.entry_mid - plan.stop) / plan.atr
    assert cfg.min_stop_atr - 1e-9 <= dist_atr <= cfg.max_stop_atr + 1e-9


def test_wider_stop_multiplier_moves_stop_further():
    narrow = _plan(Direction.LONG, PlanConfig(stop_atr_mult=1.0, max_stop_atr=4.5))
    wide = _plan(Direction.LONG, PlanConfig(stop_atr_mult=3.0, max_stop_atr=4.5))
    assert abs(wide.entry_mid - wide.stop) >= abs(narrow.entry_mid - narrow.stop)


def test_stop_percent_negative_for_long_and_positive_for_short():
    long_plan = _plan(Direction.LONG, PlanConfig())
    short_plan = _plan(Direction.SHORT, PlanConfig())
    assert long_plan.stop_pct < 0
    assert short_plan.stop_pct > 0


# ---------------------------------------------------------------------------
# Объём позиции и плечо
# ---------------------------------------------------------------------------

def test_position_size_follows_risk_percent():
    plan = _plan(Direction.LONG, PlanConfig(risk_pct=2.0))
    sizing = plan.position_size(deposit=10_000)
    assert sizing["risk_usd"] == pytest.approx(200.0)
    stop_fraction = abs(plan.stop_pct) / 100
    assert sizing["notional"] == pytest.approx(200.0 / stop_fraction, rel=1e-6)
    assert sizing["units"] == pytest.approx(sizing["notional"] / plan.entry_mid, rel=1e-6)


def test_leverage_respects_user_cap():
    # Узкий стоп → «математическое» плечо было бы огромным, потолок должен сдержать.
    plan = _plan(Direction.LONG, PlanConfig(max_leverage=2.0))
    plan.stop = plan.entry_mid * 0.995          # стоп 0.5% → 1/stop = 200x
    plan.__post_init__()
    sizing = plan.position_size(deposit=1000)
    assert sizing["leverage"] == pytest.approx(2.0)


def test_plan_from_config_carries_settings():
    from app.config.settings import Settings

    settings = Settings()
    cfg = plan_from_config(settings)
    assert cfg.risk_pct == pytest.approx(settings.risk_per_trade_pct)
    assert cfg.max_leverage == pytest.approx(settings.max_leverage)
    assert cfg.anti_chase_atr == pytest.approx(settings.anti_chase_atr)
    assert len(cfg.tp_atr_mults) == 3


# ---------------------------------------------------------------------------
# Устойчивость
# ---------------------------------------------------------------------------

def test_plan_on_degenerate_data_returns_none():
    """На мусорных данных планировщик обязан вернуть None, а не кривой план."""
    from app.domain.models import Candles, MarketSnapshot, Timeframe

    empty = Candles.from_raw("X/USDT", Timeframe.H1, [])
    snapshot = MarketSnapshot(symbol="X/USDT", base="X", quote="USDT",
                              exchange="test", candles={Timeframe.H1: empty})
    feats = build_features(snapshot, signal_tf=Timeframe.H1)
    assert build_plan(feats, Direction.LONG, PlanConfig()) is None


def test_entry_zone_is_not_above_price_for_long():
    """Для лонга зона входа не должна быть выше текущей цены (иначе это погоня)."""
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    feats = build_features(snapshot, signal_tf=features_tf())
    plan = build_plan(feats, Direction.LONG, PlanConfig())
    assert plan is not None
    price = feats.price
    # Край зоны может быть чуть выше цены, но не улетать за цену + 0.15 ATR.
    assert plan.entry_high <= price + 0.15 * plan.atr + 1e-9
    assert plan.entry_low < price
