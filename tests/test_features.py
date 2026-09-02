"""
tests/test_features.py — сбор признаков из снимка рынка.

Здесь проверяется «конвейер перед анализом»: индикаторы по таймфреймам,
уровни, метрики (сжатие, растянутость, 7д), флаги и гашение контртрендовых
идей.
"""

from __future__ import annotations

import pytest

from app.analysis.base import Group, MarketFeatures
from app.analysis.features import (build_features, collect_levels,
                                   counter_trend_damping, higher_tf_trend,
                                   round_levels, timeframe_trend_score)
from app.analysis.registry import all_modules
from app.data.synthetic import make_snapshot
from app.domain.models import Direction, MarketContext, Timeframe


@pytest.fixture
def feats() -> MarketFeatures:
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    return build_features(snapshot, signal_tf=Timeframe.H1)


def test_indicators_computed_for_all_timeframes(feats):
    assert Timeframe.H1 in feats.indicators
    assert Timeframe.H4 in feats.indicators
    assert Timeframe.D1 in feats.indicators
    primary = feats.primary()
    assert primary.bars == 520
    assert primary.rsi == primary.rsi          # не NaN
    assert primary.atr > 0
    assert 0 <= primary.stoch_k <= 100
    assert primary.ema20 != primary.ema20 or primary.ema20 > 0


def test_volatility_state_present(feats):
    assert feats.volatility.get(Timeframe.H1) is not None
    state = feats.vol(Timeframe.H1)
    assert 0.0 <= state.compression <= 1.0
    assert 0.0 <= state.atr_percentile <= 100.0


def test_structure_state_present(feats):
    struct = feats.struct(Timeframe.H1)
    assert struct is not None
    assert struct.trend in ("up", "down", "range")
    assert isinstance(struct.swings, list)


def test_metrics_contain_required_keys(feats):
    m = feats.metrics
    for key in ("change_24h", "change_7d", "distance_from_high_pct",
                "range_position", "stretch_atr", "compression",
                "atr_percentile", "htf_trend"):
        assert key in m, f"нет метрики {key}"
    assert -100 <= m["distance_from_high_pct"] <= 0
    assert 0.0 <= m["range_position"] <= 1.0


def test_funding_and_oi_in_metrics(feats):
    assert "funding_pct" in feats.metrics
    assert "oi_change_pct" in feats.metrics


def test_levels_collected_and_side_is_consistent_with_price(feats):
    assert len(feats.levels) >= 3
    for lv in feats.levels:
        if lv.kind == "support":
            assert lv.price < feats.price
        else:
            assert lv.price > feats.price
        assert lv.strength > 0
    # Ближайший уровень ищется и не «внутри» цены.
    for side in ("support", "resistance"):
        level = feats.nearest_level(side, max_distance_atr=50)
        if level is not None:
            assert level.kind == side


def test_round_levels_scale_with_price():
    assert round_levels(0.000123)[0] < 0.0002
    assert all(v > 100 for v in round_levels(12345.0))
    assert round_levels(0) == []


def test_round_levels_are_close_to_price():
    for price in (0.5, 12.34, 850.0, 67000.0):
        levels = round_levels(price, count=4)
        assert levels
        assert min(abs(v - price) for v in levels) < price * 0.5


def test_timeframe_trend_score_sign():
    snapshot = make_snapshot("TEST/USDT", "downtrend", seed=7, bars=520)
    feats = build_features(snapshot, signal_tf=Timeframe.H1)
    score, note = timeframe_trend_score(feats.primary())
    assert score < 0
    assert note

    snapshot_up = make_snapshot("TEST/USDT", "breakout", seed=7, bars=520)
    feats_up = build_features(snapshot_up, signal_tf=Timeframe.H1)
    score_up, _ = timeframe_trend_score(feats_up.primary())
    assert score_up > 0


def test_higher_tf_trend_in_range(feats):
    value = higher_tf_trend(feats)
    assert -1.0 <= value <= 1.0


def test_counter_trend_damping_reduces_opposing_signal():
    snapshot = make_snapshot("TEST/USDT", "downtrend", seed=7, bars=520)
    feats = build_features(snapshot, signal_tf=Timeframe.H1)
    htf = feats.metrics["htf_trend"]
    if htf < -0.2:
        # Лонг против нисходящего тренда гасится, шорт — нет.
        assert counter_trend_damping(feats, +1.0) < 0.99
        assert counter_trend_damping(feats, -1.0) == pytest.approx(1.0)


def test_flags_computed(feats):
    for key in ("compressed", "already_ran", "atr_ok", "stale", "illiquid"):
        assert key in feats.flags


def test_data_confidence_drops_on_stale_and_thin_data():
    from app.signals.engine import data_confidence

    good = make_snapshot("A/USDT", "range", seed=3, bars=520)
    feats_good = build_features(good, signal_tf=Timeframe.H1)
    assert data_confidence(feats_good) > 0.8

    bad = make_snapshot("B/USDT", "range", seed=3, bars=60, quote_volume=100_000)
    feats_bad = build_features(bad, signal_tf=Timeframe.H1)
    assert data_confidence(feats_bad) < data_confidence(feats_good)


def test_context_flows_into_features():
    snapshot = make_snapshot("TEST/USDT", "range", seed=5, bars=300)
    context = MarketContext(btc_score=-0.8, btc_trend="нисходящий",
                            btc_direction=Direction.SHORT)
    feats = build_features(snapshot, signal_tf=Timeframe.H1, context=context)
    assert feats.context.btc_score == -0.8


def test_empty_snapshot_produces_safe_features():
    from app.domain.models import Candles, MarketSnapshot

    empty = Candles.from_raw("X/USDT", Timeframe.H1, [])
    snapshot = MarketSnapshot(symbol="X/USDT", base="X", quote="USDT",
                              exchange="test", candles={Timeframe.H1: empty})
    feats = build_features(snapshot, signal_tf=Timeframe.H1)
    assert feats.indicators == {}
    assert feats.notes
    # Доступы не должны падать даже без данных.
    assert feats.primary().bars == 0
    assert feats.atr != feats.atr or feats.atr >= 0


def test_all_registered_modules_return_factors_or_nothing(feats):
    """Каждый модуль обязан вернуть итерируемое из Factor и не падать."""
    from app.domain.models import Factor

    for module in all_modules():
        produced = list(module(feats))
        for factor in produced:
            assert isinstance(factor, Factor)
            assert -1.0 <= factor.score <= 1.0
            assert factor.weight >= 0
            assert isinstance(factor.evidence, str)
