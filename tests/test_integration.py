"""
tests/test_integration.py — офлайн-гейт целиком и точки входа CLI.

Эти тесты медленные (секунды), зато проходят весь конвейер от синтетических
данных до готовой карточки сигнала. Если сломался хоть один слой — они краснеют.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from app.main import build_parser, cmd_modules, main
from tools.selftest import main as selftest_main


def _capture(func, *args):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = func(*args)
    return code, buffer.getvalue()


# ---------------------------------------------------------------------------
# Selftest — офлайн-гейт качества
# ---------------------------------------------------------------------------

def test_selftest_passes():
    code, output = _capture(selftest_main)
    assert code == 0, output[-3000:]
    assert "SELFTEST: все проверки пройдены" in output


def test_selftest_checks_product_thresholds():
    """Гейт обязан проверять поведение на продуктовых порогах, а не на ослабленных."""
    _, output = _capture(selftest_main)
    assert "продуктовых" in output or "продукт" in output


def test_selftest_renders_a_real_signal_card():
    _, output = _capture(selftest_main)
    assert "сигнал" in output
    assert "TP1" in output and "R:R" in output
    assert "Не является" in output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_parser_accepts_all_modes():
    parser = build_parser()
    for argv in (["--selftest"], ["--modules"], ["--analyze", "BTC"],
                 ["--scan"], ["--check"], ["--log-level", "DEBUG"]):
        args = parser.parse_args(argv)
        assert args is not None


def test_cmd_modules_lists_registry():
    code, output = _capture(cmd_modules)
    assert code == 0
    # 37 модулей в 11 группах — проверяем, что реестр не пуст и группы на месте.
    for group in ("trend", "momentum", "structure", "smc", "volume", "levels",
                  "derivatives", "context", "potential", "quality"):
        assert group in output


def test_cli_selftest_mode():
    code, output = _capture(main, ["--selftest"])
    assert code == 0
    assert "SELFTEST" in output


def test_cli_help_mentions_russian_description():
    parser = build_parser()
    text = parser.format_help()
    assert "советник" in text or "анализ" in text


def test_cli_analyze_works_offline(monkeypatch, tmp_path):
    """--analyze должен работать на синтетике, когда биржа недоступна."""
    from app.data import market as market_module

    async def fake_create(settings):
        return _FakeMarket()

    monkeypatch.setattr(market_module.MarketDataService, "create",
                        classmethod(lambda cls, settings: fake_create(settings)))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    code, output = _capture(main, ["--analyze", "TEST"])
    assert code in (0, 1)          # 1 = сигнала нет, но не падение
    assert "TEST" in output


class _FakeMarket:
    """Минимальный шлюз данных для CLI-теста: синтетические свечи."""

    def _tickers(self):
        from app.domain.models import TickerInfo

        return {
            "BTC/USDT": TickerInfo("BTC/USDT", last=60000.0, quote_volume=2e9,
                                   change_pct=1.5, high=60500.0, low=59000.0),
            "TEST/USDT": TickerInfo("TEST/USDT", last=1.4, quote_volume=2e7,
                                    change_pct=2.0, high=1.45, low=1.35),
        }

    async def tickers(self, refresh: bool = False):
        return self._tickers()

    def universe_stats(self, tickers=None):
        return {"change_24h_median": 0.5, "change_24h_std": 3.0, "count": 2}

    async def universe(self, limit=None):
        return ["BTC/USDT", "TEST/USDT"]

    async def snapshot(self, symbol, tickers=None, stats=None, **kwargs):
        from app.data.synthetic import make_snapshot

        return make_snapshot(symbol, "breakout", seed=21, bars=520)

    async def candles(self, symbol, timeframe, limit=300):
        from app.data.synthetic import make_snapshot
        from app.domain.models import Timeframe

        snap = make_snapshot(symbol, "breakout", seed=21, bars=300)
        return snap.candles[Timeframe.H1]

    async def market_context(self, tickers=None, universe=None):
        from app.domain.models import MarketContext

        return MarketContext()

    def health(self):
        return ["fake: офлайн"]

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Бектест-харнесс нового движка
# ---------------------------------------------------------------------------

def test_backtest_harness_produces_trades_and_metrics():
    """
    Харнесс должен исполнять планы и считать метрики.

    Здесь же живёт регрессия на свежесть данных: историческое окно всегда
    «в прошлом», и если возраст считать от открытия свечи, гейт свежести
    отвергает все сигналы и сделок получается ноль.
    """
    from app.config.settings import Settings
    from app.signals.engine import SignalEngine
    from tools.backtest_engine import run_walk, summarise, synthetic_frames

    settings = Settings()
    settings.bars_base = 500
    settings.min_confidence = 4.0
    settings.min_rr = 1.2
    engine = SignalEngine(settings)

    frames = synthetic_frames(["breakout"], [1], bars=1600)
    assert frames
    symbol, frame = frames[0]
    walk = run_walk(symbol, frame, engine, settings, window_step=48,
                    horizon_bars=48)
    assert walk.signals > 10, "окон должно быть больше десятка"
    assert walk.trades, "на синтетике с дрейфом должны исполняться сделки"

    summary = summarise([walk], symbol)
    assert summary["trades"] == len(walk.trades)
    assert 0.0 <= summary["win_rate"] <= 100.0
    assert summary["profit_factor"] > 0
    assert summary["calibration"], "должна быть хотя бы одна корзина уверенности"
    for trade in walk.trades:
        assert trade.outcome in ("STOP", "TP1", "TP2", "TP3", "EXPIRED")
        assert trade.bars_held >= 0


def test_backtest_report_renders():
    from tools.backtest_engine import render_report

    rows = [{"label": "A", "signals": 10, "trades": 2, "unfilled": 1,
             "wait_or_no_plan": 7, "fill_rate": 66.7, "win_rate": 50.0,
             "avg_r": 0.5, "total_r": 1.0, "profit_factor": 1.5, "max_dd_r": 0.8,
             "avg_bars": 5.0,
             "calibration": {"5–6.5": {"n": 2, "win_rate": 50.0, "avg_r": 0.5}}}]
    text = render_report(rows, "тест")
    assert "| A | 10 | 2 |" in text
    assert "Калибровка" in text
    assert "Не является" in text or "вероятность прибыли" in text


def test_backtest_cli_rejects_missing_input():
    from tools.backtest_engine import main as backtest_main

    assert backtest_main([]) == 2
    assert backtest_main(["--source", "/нет/такого/каталога"]) == 2
