"""
Offline-тесты квантового ядра v3 (strategy.py) и его интеграции в сканер.

Покрывает:
  * SpreadSeries — скользящая статистика (O(1)-оптимизация против прямого
    подсчёта), z-score, перцентиль, обрезка окна, полужизнь AR(1);
  * funding edge — знак для S>F / F>S;
  * SignalEngine — гейты CARRY / REVERSION / persistence / fixed-режим;
  * assess_snapshot — не пишет в историю;
  * EpisodeTracker — открытие/закрытие эпизодов, winrate;
  * интеграция: _observe_engine кормит движок обоими направлениями,
    /stats собирается, /top показывает колонку Z·ГРЕЙД,
    авто-пуш в adaptive отклоняет неподтверждённые сигналы;
  * config — парсинг новых переменных окружения.

Запуск: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Settings                                     # noqa: E402
from strategy import (                                          # noqa: E402
    Assessment,
    EpisodeTracker,
    SignalEngine,
    SpreadSeries,
    StrategyConfig,
    funding_edge_percent,
)

from test_scanner import (                                      # noqa: E402
    FakeNotifier,
    make_quote,
    make_scanner,
    make_settings,
    make_side,
)


# ---------------------------------------------------------------------------
# SpreadSeries
# ---------------------------------------------------------------------------

class TestSpreadSeries(unittest.TestCase):
    def test_stats_match_direct_computation(self):
        series = SpreadSeries(max_samples=100, window_seconds=10_000.0)
        values = [0.10, 0.20, 0.15, 0.30, 0.05, 0.25, 0.18, 0.22, 0.11, 0.19]
        for i, v in enumerate(values):
            series.add(i * 10.0, v)
        stats = series.stats()
        n = len(values)
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        self.assertEqual(stats.n, n)
        self.assertAlmostEqual(stats.mean, mean, places=12)
        self.assertAlmostEqual(stats.std, var ** 0.5, places=12)
        self.assertAlmostEqual(stats.min, min(values))
        self.assertAlmostEqual(stats.max, max(values))

    def test_zscore_and_percentile(self):
        series = SpreadSeries(max_samples=100, window_seconds=10_000.0)
        values = [float(i) for i in range(1, 21)]  # 1..20
        for i, v in enumerate(values):
            series.add(i * 10.0, v)
        stats = series.stats()
        z = series.zscore(14.5, stats)  # медиана → z ≈ 0
        self.assertAlmostEqual(z, (14.5 - stats.mean) / stats.std, places=12)
        # перцентиль: все значения ниже 21 → 100; ниже 0.5 → 0
        self.assertAlmostEqual(series.percentile_rank(21.0), 100.0)
        self.assertAlmostEqual(series.percentile_rank(0.5), 0.0)
        # строго ниже 15 → 14 из 20 = 70
        self.assertAlmostEqual(series.percentile_rank(15.0), 70.0)

    def test_window_trim_by_time(self):
        series = SpreadSeries(max_samples=1000, window_seconds=100.0)
        for i in range(50):
            series.add(i * 10.0, 0.1)  # шаг 10с, окно 100с → хвост ~10 сэмплов
        self.assertLessEqual(len(series._samples), 11)
        self.assertLessEqual(series.stats().n, 11)

    def test_halflife_of_mean_reverting_series(self):
        import random
        random.seed(42)
        series = SpreadSeries(max_samples=500, window_seconds=1e9)
        value = 0.0
        # OU-процесс: возврат к 0 с коэффициентом 0.5 + шум
        for i in range(200):
            value = 0.5 * value + random.gauss(0.0, 0.1)
            series.add(i * 3600.0, value)
        stats = series.stats(compute_halflife=True)
        self.assertIsNotNone(stats.halflife_hours)
        # phi≈0.5 → полужизнь ≈ 1 шаг = 1 час (допускаем вилку 0.5–2.5 ч)
        self.assertGreater(stats.halflife_hours, 0.5)
        self.assertLess(stats.halflife_hours, 2.5)

    def test_halflife_none_for_flat_series(self):
        series = SpreadSeries(max_samples=100, window_seconds=1e9)
        for i in range(20):
            series.add(i * 3600.0, 0.5)
        self.assertIsNone(series.stats(compute_halflife=True).halflife_hours)


# ---------------------------------------------------------------------------
# Funding edge
# ---------------------------------------------------------------------------

class TestFundingEdge(unittest.TestCase):
    def test_carry_signs(self):
        # funding > 0: лонги платят шортам → S>F (шорт перпа) получает
        self.assertAlmostEqual(funding_edge_percent(0.01, True, 24.0), 0.03)
        # F>S (лонг перпа) при положительной ставке платит
        self.assertAlmostEqual(funding_edge_percent(0.01, False, 24.0), -0.03)
        # отрицательная ставка: наоборот
        self.assertAlmostEqual(funding_edge_percent(-0.02, False, 8.0), 0.02)
        self.assertAlmostEqual(funding_edge_percent(None, True, 24.0), 0.0)


# ---------------------------------------------------------------------------
# SignalEngine: классы сигналов и гейты
# ---------------------------------------------------------------------------

def adaptive_cfg(**over) -> StrategyConfig:
    base = dict(
        mode="adaptive",
        history_seconds=100 * 3600.0,
        min_history=20,
        min_persistence=2,
        z_entry=1.5,
        z_entry_min=-1.0,
        pct_entry=85.0,
        min_net_roundtrip_percent=0.10,
        min_funding_edge_percent=0.30,
        min_net_reversion_percent=0.20,
        horizon_hours=240.0,
        max_samples=500,
    )
    base.update(over)
    return StrategyConfig(**base)


class TestSignalEngine(unittest.TestCase):
    def _warm(self, engine: SignalEngine, key: str, values, funding=None):
        assessment = None
        for i, v in enumerate(values):
            assessment = engine.observe_and_assess(
                key, i * 3600.0, v, v + 0.15, True,
                funding_rate_percent=funding, fillable_usd=5000.0,
            )
        return assessment

    def test_reversion_signal_on_anomaly(self):
        engine = SignalEngine(adaptive_cfg())
        calm = [0.02 + 0.01 * ((-1) ** i) for i in range(40)]
        self._warm(engine, "BTC|a|b|S>F", calm)
        a = self._warm(engine, "BTC|a|b|S>F", [0.6, 0.7, 0.8])
        self.assertTrue(a.actionable)
        self.assertIn("REVERSION", a.signal_class)
        self.assertGreaterEqual(a.confidence, 65)
        self.assertGreater(a.zscore, 1.5)

    def test_carry_signal_on_funding(self):
        engine = SignalEngine(adaptive_cfg())
        import random
        random.seed(7)
        calm = [0.0 + random.gauss(0, 0.05) for _ in range(40)]
        a = self._warm(engine, "ETH|a|b|S>F", calm, funding=0.02)  # 0.02%/8ч
        self.assertTrue(a.actionable)
        self.assertIn("CARRY", a.signal_class)
        self.assertGreaterEqual(a.funding_edge_percent, 0.30)

    def test_no_signal_without_history(self):
        engine = SignalEngine(adaptive_cfg(min_history=50))
        a = self._warm(engine, "X|a|b|S>F", [0.5] * 10, funding=0.05)
        self.assertFalse(a.actionable)
        self.assertIn("истории", "; ".join(a.reasons))

    def test_persistence_blocks_single_spike(self):
        engine = SignalEngine(adaptive_cfg(min_persistence=3))
        calm = [0.02 + 0.01 * ((-1) ** i) for i in range(40)]
        self._warm(engine, "Y|a|b|S>F", calm)
        a = self._warm(engine, "Y|a|b|S>F", [0.9, 0.9])  # только 2 подряд
        self.assertFalse(a.actionable)
        self.assertIn("подтверждений", "; ".join(a.reasons))

    def test_carry_allows_moderately_negative_spread(self):
        # CARRY не требует положительного спреда: при жирном funding вход
        # возможен и при умеренно отрицательном (z в норме) спреде пары
        engine = SignalEngine(adaptive_cfg())
        values = [-0.35 + (0.02 if i % 2 == 0 else -0.02) for i in range(40)]
        a = self._warm(engine, "Z|a|b|S>F", values, funding=0.03)
        self.assertTrue(a.actionable)
        self.assertEqual(a.signal_class, "CARRY")

    def test_carry_blocked_when_spread_anomalously_low(self):
        # ...но если текущий спред аномально НИЖЕ своей нормы (z < z_entry_min),
        # funding-вход блокируется: ждём возврата к норме пары
        engine = SignalEngine(adaptive_cfg())
        values = [-0.35 + 0.02 * ((-1) ** i) for i in range(40)]
        values += [-0.9, -0.95, -1.0]  # аномальное падение спреда
        a = self._warm(engine, "Z|a|b|S>F", values, funding=0.03)
        self.assertFalse(a.actionable)
        self.assertIn("вывернут", "; ".join(a.reasons))

    def test_fixed_mode_uses_flat_threshold(self):
        engine = SignalEngine(StrategyConfig(mode="fixed", min_spread_percent=2.0))
        a = self._warm(engine, "F|a|b|S>F", [3.0] * 5)
        self.assertTrue(a.actionable)
        b = self._warm(engine, "F|a|b|S>F", [1.0] * 5)
        self.assertFalse(b.actionable)

    def test_snapshot_does_not_mutate_history(self):
        engine = SignalEngine(adaptive_cfg())
        self._warm(engine, "S|a|b|S>F", [0.1] * 30)
        before_n = engine.series("S|a|b|S>F").stats().n
        snap = engine.assess_snapshot(
            "S|a|b|S>F", 0.9, 1.05, True, funding_rate_percent=0.05,
            fillable_usd=5000.0,
        )
        after_n = engine.series("S|a|b|S>F").stats().n
        self.assertEqual(before_n, after_n)
        self.assertIsInstance(snap, Assessment)
        # snapshot видит ту же статистику
        self.assertEqual(snap.stats.n, before_n)

    def test_disable_classes(self):
        engine = SignalEngine(adaptive_cfg(enable_reversion=False))
        calm = [0.02 + 0.01 * ((-1) ** i) for i in range(40)]
        self._warm(engine, "R|a|b|S>F", calm)
        a = self._warm(engine, "R|a|b|S>F", [0.6, 0.7, 0.8])
        self.assertFalse(a.actionable)  # REV выключен, funding нет → тишина


# ---------------------------------------------------------------------------
# EpisodeTracker
# ---------------------------------------------------------------------------

class TestEpisodeTracker(unittest.TestCase):
    def _assessment(self, net: float, z: float, actionable: bool) -> Assessment:
        return Assessment(
            key="BTC|a|b|S>F", net_spread_percent=net, zscore=z,
            actionable=actionable,
        )

    def test_open_close_and_winrate(self):
        tracker = EpisodeTracker(exit_fee_percent=0.15, z_exit=0.0)
        tracker.update(self._assessment(1.0, 3.0, True), now=0.0)      # вход
        tracker.update(self._assessment(0.4, 0.5, False), now=3600.0)  # сходимость
        tracker.update(self._assessment(0.1, 0.0, False), now=7200.0)  # выход
        summary = tracker.summary()
        self.assertEqual(summary["episodes"], 1)
        self.assertEqual(summary["winrate"], 100.0)
        # захват = (1.0 − 0.1) − 0.15 = 0.75
        self.assertAlmostEqual(summary["avg_pnl_percent"], 0.75)

    def test_timeout_close(self):
        tracker = EpisodeTracker(exit_fee_percent=0.15, z_exit=0.0, max_episode_hours=10.0)
        tracker.update(self._assessment(0.5, 2.0, True), now=0.0)
        tracker.update(self._assessment(0.5, 2.0, False), now=11 * 3600.0)
        self.assertEqual(tracker.summary()["episodes"], 1)


# ---------------------------------------------------------------------------
# Интеграция со сканером (adaptive-режим)
# ---------------------------------------------------------------------------

class TestQuantIntegration(unittest.TestCase):
    def _scanner(self, **over):
        settings = make_settings(strategy_mode="adaptive", **over)
        spot = make_side("mexc", "spot", {
            "BTC": make_quote(99.0, 100.0),
            "ETH": make_quote(9.9, 10.0),
        })
        fut = make_side("bybit", "futures", {
            "BTC": make_quote(101.0, 102.0),
            "ETH": make_quote(10.11, 10.12),
        })
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        return scanner

    def test_observe_engine_feeds_both_directions(self):
        scanner = self._scanner()
        scanner._observe_engine(time.time())
        keys = set(scanner.signal_engine._series)
        self.assertTrue(any(k.endswith("|S>F") for k in keys))
        self.assertTrue(any(k.endswith("|F>S") for k in keys))
        for srs in scanner.signal_engine._series.values():
            self.assertEqual(srs.stats().n, 1)

    def test_stats_message_builds(self):
        scanner = self._scanner()
        for _ in range(3):
            scanner._observe_engine(time.time())
        message = asyncio_run(scanner._cmd_stats("1", ""))
        self.assertIn("КВАНТОВОЕ ЯДРО", message)

    def test_top_message_has_quant_column(self):
        scanner = self._scanner()
        # прогрев истории, чтобы оценка появилась
        for _ in range(70):
            scanner._observe_engine(time.time())
        html = asyncio_run(scanner._cmd_top("1", ""))
        self.assertIn("Z·ГРЕЙД", html)

    def test_auto_push_rejected_without_confirmation(self):
        scanner = self._scanner(max_signals_per_scan=5)
        scanner.settings = scanner.settings  # signal_mode зададим ниже
        object.__setattr__(scanner.settings, "signal_mode", "auto")
        # истории нет → движок не подтвердит → авто-пуш молчит
        for _ in range(3):
            scanner._observe_engine(time.time())
        pairs = scanner._evaluate()
        asyncio_run(scanner._process_signals(pairs))
        notifier = scanner.notifier
        self.assertEqual(notifier.messages, [])
        self.assertGreater(scanner.stats["signals_rejected_quant"], 0)

    def test_auto_push_fixed_mode_unchanged(self):
        # в fixed-режиме авто-пуш работает как раньше (плоский порог)
        settings = make_settings(
            strategy_mode="fixed", min_spread_percent=0.5, signal_mode="auto",
            max_signals_per_scan=5,
        )
        spot = make_side("mexc", "spot", {"BTC": make_quote(99.0, 100.0)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(103.0, 104.0)})
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        pairs = scanner._evaluate()
        asyncio_run(scanner._process_signals(pairs))
        self.assertEqual(len(scanner.notifier.messages), 1)


def asyncio_run(coro):
    return asyncio.run(coro)


import asyncio  # noqa: E402  (используется хелпером выше)


# ---------------------------------------------------------------------------
# Конфигурация: новые переменные окружения
# ---------------------------------------------------------------------------

class TestStrategyConfigEnv(unittest.TestCase):
    def test_new_env_variables_parsed(self):
        env = {
            "STRATEGY_MODE": "adaptive",
            "Z_ENTRY": "2.5",
            "Z_ENTRY_MIN": "-0.5",
            "PCT_ENTRY": "95",
            "MIN_NET_ROUNDTRIP_PERCENT": "0.2",
            "MIN_FUNDING_EDGE_PERCENT": "0.4",
            "MIN_NET_REVERSION_PERCENT": "0.3",
            "MIN_PERSISTENCE": "4",
            "MIN_HISTORY": "120",
            "HISTORY_SECONDS": "7200",
            "FUNDING_HORIZON_HOURS": "120",
            "MIN_FILLABLE_USD": "250",
            "CONFIDENCE_MIN_PUSH": "70",
            "Z_EXIT": "0.2",
            "MAX_EPISODE_HOURS": "300",
        }
        old = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            s = Settings.from_env()
            self.assertEqual(s.strategy_mode, "adaptive")
            self.assertAlmostEqual(s.z_entry, 2.5)
            self.assertAlmostEqual(s.z_entry_min, -0.5)
            self.assertAlmostEqual(s.pct_entry, 95.0)
            self.assertAlmostEqual(s.min_net_roundtrip_percent, 0.2)
            self.assertAlmostEqual(s.min_funding_edge_percent, 0.4)
            self.assertAlmostEqual(s.min_net_reversion_percent, 0.3)
            self.assertEqual(s.min_persistence, 4)
            self.assertEqual(s.min_history, 120)
            self.assertAlmostEqual(s.history_seconds, 7200.0)
            self.assertAlmostEqual(s.funding_horizon_hours, 120.0)
            self.assertAlmostEqual(s.min_fillable_usd, 250.0)
            self.assertEqual(s.confidence_min_push, 70)
            self.assertAlmostEqual(s.z_exit, 0.2)
            self.assertAlmostEqual(s.max_episode_hours, 300.0)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_invalid_strategy_mode_rejected(self):
        old = os.environ.get("STRATEGY_MODE")
        os.environ["STRATEGY_MODE"] = "chaos"
        try:
            with self.assertRaises(ValueError):
                Settings.from_env()
        finally:
            if old is None:
                os.environ.pop("STRATEGY_MODE", None)
            else:
                os.environ["STRATEGY_MODE"] = old


if __name__ == "__main__":
    unittest.main()
