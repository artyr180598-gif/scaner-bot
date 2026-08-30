"""Offline-тесты направленного слоя PULSE (alpha.py)."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha import (  # noqa: E402
    PulseConfig,
    PulseEngine,
    ema_last,
    rsi_last,
    tanh_clip,
    zscore_last,
)


class TestIndicators(unittest.TestCase):
    def test_rsi_flat_is_near_50(self):
        closes = [100.0 + (0.2 if i % 2 == 0 else -0.2) for i in range(80)]
        rsi = rsi_last(closes, 14)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 40.0)
        self.assertLess(rsi, 60.0)

    def test_rsi_uptrend_is_high(self):
        closes = [100.0 + i for i in range(40)]
        rsi = rsi_last(closes, 14)
        self.assertGreater(rsi, 80.0)

    def test_ema_follows_level(self):
        values = [10.0] * 30
        self.assertAlmostEqual(ema_last(values, 10), 10.0, places=6)

    def test_zscore_last_extreme(self):
        xs = [0.0] * 50 + [5.0]
        z = zscore_last(xs, 50)
        self.assertGreater(z, 3.0)

    def test_tanh_clip_bounds(self):
        self.assertGreater(tanh_clip(10.0), 0.99)
        self.assertLess(tanh_clip(-10.0), -0.99)
        self.assertEqual(tanh_clip(0.0), 0.0)


class TestPulseEngine(unittest.TestCase):
    def test_warmup_not_actionable(self):
        eng = PulseEngine(PulseConfig(min_bars=30))
        snap = None
        for i in range(10):
            px = 100.0 + i * 0.1
            snap = eng.observe_bar("BTC", i * 3600, px, px + 0.2, px - 0.2, px, 1.0, 0.01, 0.05)
        self.assertFalse(snap.actionable)
        self.assertIn("мало баров", ";".join(snap.reasons))

    def test_observe_quote_aggregates_bars(self):
        eng = PulseEngine(bar_seconds=100.0)
        eng.observe_quote("ETH", 10.0, 100.0)
        eng.observe_quote("ETH", 50.0, 102.0)
        snap = eng.observe_quote("ETH", 110.0, 101.0)  # закрывает бар 0
        self.assertGreaterEqual(snap.n_bars, 1)
        tape = eng.tape("ETH")
        self.assertEqual(tape.high[-1], 102.0)
        self.assertEqual(tape.low[-1], 100.0)
        self.assertEqual(tape.close[-1], 102.0)

    def test_crowding_fade_short_when_funding_extreme(self):
        eng = PulseEngine(PulseConfig(min_bars=60, score_entry=0.20, crowding_extreme=1.5))
        price = 100.0
        for i in range(80):
            # спокойный funding, потом всплеск
            fund = 0.001 if i < 70 else 0.08
            price += 0.05
            snap = eng.observe_bar(
                "SOL", i * 14400.0, price, price + 0.3, price - 0.3, price,
                10.0, fund, 0.2,
            )
        self.assertIsNotNone(snap.funding_z)
        self.assertGreater(snap.funding_z, 1.5)
        # fade crowding → шорт (score < 0)
        self.assertLess(snap.score, 0.0)

    def test_rank_orders_by_abs_score(self):
        eng = PulseEngine(PulseConfig(min_bars=5))
        for sym, drift in (("AAA", 1.0), ("BBB", -0.2)):
            px = 50.0
            for i in range(70):
                px += drift
                eng.observe_bar(sym, i * 3600, px, px + 0.1, px - 0.1, px, 1.0, 0.0, 0.0)
        ranked = eng.rank(limit=2)
        self.assertEqual(len(ranked), 2)
        self.assertGreaterEqual(abs(ranked[0].score), abs(ranked[1].score))


if __name__ == "__main__":
    unittest.main()
