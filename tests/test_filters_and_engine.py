"""
tests/test_filters_and_engine.py — гейты качества и сквозной анализ.

Гейты — это то, что отличает продукт от генератора мусорных сигналов:
проверяем, что каждый из них реально срабатывает и объясняет отказ.
Сквозной тест гоняет полный конвейер на синтетике.
"""

from __future__ import annotations

import pytest

from app.analysis.features import build_features
from app.data.synthetic import make_snapshot
from app.domain.models import (Direction, Factor, FactorSet, MarketContext,
                               Timeframe)
from app.signals.engine import SignalEngine, data_confidence
from app.signals.explain import beginner_note, build_summary, detect_setup
from app.signals.filters import FilterConfig, apply_filters
from app.signals.planner import PlanConfig, build_plan
from app.scoring.scorer import score_factors


def _make(features, direction=Direction.LONG, confidence=8.0, score=0.6,
          potential=0.7, agreement=4, data_confidence=1.0):
    fs = FactorSet(
        factors=[
            Factor("t", "trend", 0.9, "тренд вверх"),
            Factor("s", "structure", 0.8, "пробой структуры"),
            Factor("p", "potential", potential, "сжатие"),
        ],
        data_confidence=data_confidence,
    )
    res = score_factors(fs)
    res.confidence = confidence
    res.score = score
    res.potential = potential
    res.agreement = agreement
    res.direction = direction
    plan = build_plan(features, direction, PlanConfig())
    return res, plan


@pytest.fixture
def feats():
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    return build_features(snapshot, signal_tf=Timeframe.H1)


# ---------------------------------------------------------------------------
# Гейты
# ---------------------------------------------------------------------------

def test_all_gates_pass(feats):
    """Счастливый путь: ни один гейт не срабатывает.

    ``min_rr`` задаём явно: синтетический снимок привязан к «сейчас», поэтому
    набор магнитов (и средний R:R плана) слегка гуляет от момента запуска,
    а смысл теста — проверить, что гейты пропускают валидный сигнал.
    """
    res, plan = _make(feats)
    verdict = apply_filters(res, plan, feats,
                            FilterConfig(min_quote_volume_usd=1_000, min_rr=1.2))
    assert verdict.accepted, verdict.reasons
    assert verdict.direction is Direction.LONG


def test_wait_direction_rejected(feats):
    res, plan = _make(feats, direction=Direction.WAIT)
    verdict = apply_filters(res, plan, feats, FilterConfig())
    assert not verdict.accepted
    assert verdict.direction is Direction.WAIT


def test_low_confidence_rejected_with_reason(feats):
    res, plan = _make(feats, confidence=3.0)
    verdict = apply_filters(res, plan, feats, FilterConfig(min_confidence=6.0))
    assert not verdict.accepted
    assert any("уверенность" in r for r in verdict.reasons)


def test_low_potential_rejected(feats):
    res, plan = _make(feats, potential=0.05)
    verdict = apply_filters(res, plan, feats, FilterConfig(min_potential=0.3))
    assert not verdict.accepted
    assert any("потенциал" in r for r in verdict.reasons)


def test_single_group_agreement_rejected(feats):
    res, plan = _make(feats, agreement=1)
    verdict = apply_filters(res, plan, feats, FilterConfig(min_agreement=2))
    assert not verdict.accepted
    assert any("независим" in r for r in verdict.reasons)


def test_illiquid_symbol_rejected(feats):
    feats.snapshot.ticker.quote_volume = 50_000
    res, plan = _make(feats)
    verdict = apply_filters(res, plan, feats,
                            FilterConfig(min_quote_volume_usd=3_000_000))
    assert not verdict.accepted
    assert any("неликвид" in r for r in verdict.reasons)


def test_bad_data_confidence_rejected(feats):
    res, plan = _make(feats, data_confidence=0.2)
    res.data_confidence = 0.2
    verdict = apply_filters(res, plan, feats, FilterConfig(min_data_confidence=0.55))
    assert not verdict.accepted
    assert any("качество данных" in r for r in verdict.reasons)


def test_missing_plan_rejected(feats):
    res, _ = _make(feats)
    verdict = apply_filters(res, None, feats, FilterConfig(min_quote_volume_usd=1_000))
    assert not verdict.accepted
    assert any("план" in r for r in verdict.reasons)


def test_bad_rr_rejected(feats):
    res, plan = _make(feats)
    # Цели почти у входа → R:R никуда не годится.
    for target in plan.targets:
        target.price = plan.entry_mid + 0.0001
    plan.__post_init__()
    verdict = apply_filters(res, plan, feats,
                            FilterConfig(min_rr=1.5, min_quote_volume_usd=1_000))
    assert not verdict.accepted
    assert any("R:R" in r for r in verdict.reasons)


def test_anti_chase_marks_soft_rejection(feats):
    """Цена ушла от зоны входа — не «нет сигнала», а «жди откат»."""
    res, plan = _make(feats)
    plan.entry_low = plan.entry_mid * 0.90
    plan.entry_high = plan.entry_mid * 0.92
    plan.__post_init__()
    verdict = apply_filters(res, plan, feats,
                            FilterConfig(anti_chase_atr=1.0,
                                         min_quote_volume_usd=1_000))
    assert not verdict.accepted
    assert verdict.hard is False
    assert any("откат" in r for r in verdict.reasons)


# ---------------------------------------------------------------------------
# Сквозной анализ
# ---------------------------------------------------------------------------

def test_engine_returns_signal_for_strong_setup():
    settings = _loose_settings()
    engine = SignalEngine(settings)
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    signal = engine.analyze(snapshot, MarketContext())
    assert signal.actionable
    assert signal.direction is Direction.LONG
    assert signal.plan.is_valid()
    assert signal.confidence >= settings.min_confidence
    assert len(signal.plan.targets) == 3
    assert signal.summary


def test_engine_does_not_recommend_pumped_coin():
    engine = SignalEngine(_loose_settings())
    snapshot = make_snapshot("TEST/USDT", "pumped", seed=7, bars=520)
    signal = engine.analyze(snapshot, MarketContext())
    assert not signal.actionable
    assert "уже росла" in signal.tags or signal.direction is Direction.WAIT


def test_engine_wait_signal_explains_itself():
    engine = SignalEngine(_loose_settings())
    snapshot = make_snapshot("TEST/USDT", "accumulation", seed=7, bars=520)
    signal = engine.analyze(snapshot, MarketContext())
    assert signal.direction is Direction.WAIT
    assert "Почему не сигнал" in signal.summary or "Ждём" in signal.summary
    assert signal.plan is None


def test_context_shifts_score():
    """Бычий BTC усиливает лонг по альту, медвежий — ослабляет."""
    engine = SignalEngine(_loose_settings())
    snapshot = make_snapshot("ALT/USDT", "breakout", seed=21, bars=520)
    bull = engine.analyze(snapshot, MarketContext(btc_score=0.8, btc_trend="восходящий",
                                                  btc_direction=Direction.LONG))
    bear = engine.analyze(snapshot, MarketContext(btc_score=-0.8, btc_trend="нисходящий",
                                                  btc_direction=Direction.SHORT))
    assert bull.score > bear.score


def test_signal_metrics_and_tags_present():
    engine = SignalEngine(_loose_settings())
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    signal = engine.analyze(snapshot, MarketContext())
    assert signal.metrics
    assert signal.data_confidence > 0.5
    assert signal.timeframe == Timeframe.H1
    assert signal.to_dict()["symbol"] == "TEST/USDT"


def test_analyze_many_is_crash_safe():
    engine = SignalEngine(_loose_settings())
    snapshots = [make_snapshot("A/USDT", "range", seed=1, bars=300),
                 make_snapshot("B/USDT", "breakout", seed=2, bars=300)]
    signals = engine.analyze_many(snapshots, MarketContext())
    assert len(signals) == 2


def test_setup_and_summary_helpers():
    engine = SignalEngine(_loose_settings())
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    signal = engine.analyze(snapshot, MarketContext())
    assert signal.setup
    note = beginner_note(signal)
    assert "R:R" in note and "Не является" not in note
    assert "риск" in note.lower() or "Риск" in note


def _loose_settings():
    from app.config.settings import Settings

    s = Settings()
    s.min_confidence = 4.0
    s.min_rr = 1.1
    s.min_quote_volume_usd = 100_000
    return s
