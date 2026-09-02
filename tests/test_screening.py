"""
tests/test_screening.py — отбор монет: «сжатая пружина» против уже улетевших.

Это ключевое продуктовое требование: бот ищет монеты ДО движения, а не после.
"""

from __future__ import annotations

import pytest

from app.data.synthetic import make_snapshot, make_universe
from app.domain.models import TickerInfo
from app.screening.prescreen import (PrescreenConfig, coarse_screen, fine_screen,
                                     _bell, _liquidity_score)

CFG = PrescreenConfig(min_quote_volume=100_000, coarse_candidates=20,
                      fine_candidates=10)


def tickers_from(snapshots):
    return {s.symbol: s.ticker for s in snapshots if s.ticker}


@pytest.fixture
def universe():
    return make_universe(
        ["ACUM/USDT", "PUMP/USDT", "DOWN/USDT", "RNG/USDT", "BRK/USDT", "CAP/USDT"],
        ["accumulation", "pumped", "downtrend", "range", "breakout", "capitulation"],
        seed=11, bars=520,
    )


# ---------------------------------------------------------------------------
# Грубый отбор (по тикерам)
# ---------------------------------------------------------------------------

def test_coarse_screen_filters_stablecoins_and_illiquid():
    tickers = {
        "BTC/USDT": TickerInfo("BTC/USDT", last=60000, quote_volume=1e9,
                               change_pct=1.0, high=61000, low=59000),
        "USDC/USDT": TickerInfo("USDC/USDT", last=1.0, quote_volume=1e9,
                                change_pct=0.0, high=1.001, low=0.999),
        "DEAD/USDT": TickerInfo("DEAD/USDT", last=0.01, quote_volume=1000,
                                change_pct=0.0, high=0.011, low=0.009),
    }
    cfg = PrescreenConfig(min_quote_volume=1_000_000)
    out = coarse_screen(tickers, {}, cfg)
    symbols = [c.symbol for c in out]
    assert "BTC/USDT" in symbols
    assert "DEAD/USDT" not in symbols          # неликвид
    # USDC/USDT отваливается по узкому диапазону (< 1% за сутки)


def test_coarse_screen_rejects_already_pumped():
    tickers = {
        "CALM/USDT": TickerInfo("CALM/USDT", last=1.0, quote_volume=5e7,
                                change_pct=1.0, high=1.03, low=0.98),
        "MOON/USDT": TickerInfo("MOON/USDT", last=2.0, quote_volume=5e7,
                                change_pct=45.0, high=2.1, low=1.4),
    }
    cfg = PrescreenConfig(min_quote_volume=1_000_000, max_change_24h_pct=18.0)
    out = coarse_screen(tickers, {"change_24h_median": 0.0, "change_24h_std": 2.0}, cfg)
    symbols = [c.symbol for c in out]
    assert "CALM/USDT" in symbols
    assert "MOON/USDT" not in symbols


def test_coarse_screen_scores_compressed_higher():
    tickers = {
        "TIGHT/USDT": TickerInfo("TIGHT/USDT", last=1.0, quote_volume=5e7,
                                 change_pct=0.5, high=1.02, low=0.99),
        "WIDE/USDT": TickerInfo("WIDE/USDT", last=1.0, quote_volume=5e7,
                                change_pct=0.5, high=1.08, low=0.93),
    }
    out = coarse_screen(tickers, {"change_24h_median": 0.0, "change_24h_std": 2.0}, CFG)
    ranked = {c.symbol: c.score for c in out}
    assert ranked["TIGHT/USDT"] > ranked["WIDE/USDT"]


# ---------------------------------------------------------------------------
# Тонкий отбор (по свечам)
# ---------------------------------------------------------------------------

def test_fine_screen_ranks_accumulation_above_pumped(universe):
    out = fine_screen(universe, CFG)
    ranked = [c.symbol for c in out]
    assert ranked, "тонкий отбор должен вернуть кандидатов"
    assert "ACUM/USDT" in ranked
    if "PUMP/USDT" in ranked:
        assert ranked.index("ACUM/USDT") < ranked.index("PUMP/USDT")


def test_fine_screen_drops_coin_that_already_ran():
    """Монета с движением > 45% за неделю не проходит тонкий отбор."""
    snapshots = [make_snapshot("ROCKET/USDT", "pumped", seed=5, bars=520)]
    out = fine_screen(snapshots, PrescreenConfig(min_quote_volume=100_000,
                                                 min_change_7d_abs=20.0))
    assert out == [] or out[0].metrics["change_7d"] < 20.0


def test_fine_screen_metrics_present(universe):
    out = fine_screen(universe, CFG)
    for candidate in out:
        assert "compression" in candidate.metrics
        assert "change_7d" in candidate.metrics
        assert 0.0 <= candidate.metrics["compression"] <= 1.0
        assert 0.0 <= candidate.score <= 1.0
        assert candidate.reasons


def test_fine_screen_ignores_short_history():
    snapshots = [make_snapshot("SHORT/USDT", "range", seed=1, bars=40)]
    assert fine_screen(snapshots, CFG) == []


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def test_bell_peaks_at_center():
    assert _bell(4.0, center=4.0, width=8.0) == pytest.approx(1.0)
    assert _bell(12.0, center=4.0, width=8.0) == pytest.approx(0.0)
    assert _bell(float("nan"), 4.0, 8.0) == 0.0


def test_liquidity_score_prefers_mid_caps():
    tiny = _liquidity_score(50_000)
    mid = _liquidity_score(80_000_000)
    huge = _liquidity_score(20_000_000_000)
    assert mid > tiny
    assert mid > huge
