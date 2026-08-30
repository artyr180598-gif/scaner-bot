"""Офлайн-тесты forge.ForgeEngine (LOW_CHAN)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forge import ForgeConfig, ForgeEngine  # noqa: E402


class TestForgeEngine(unittest.TestCase):
    def test_warmup(self) -> None:
        eng = ForgeEngine(ForgeConfig(min_bars=90))
        snap = eng.observe_bar("AAA", 0, 10, 10.1, 9.9, 10, 1)
        self.assertFalse(snap.picked)
        self.assertIn("мало баров", " ".join(snap.reasons))

    def test_quote_aggregates_day(self) -> None:
        eng = ForgeEngine(bar_seconds=100.0)
        eng.observe_quote("ETH", 10, 100)
        eng.observe_quote("ETH", 50, 105)
        eng.observe_quote("ETH", 110, 101)
        tape = eng.tape("ETH")
        self.assertGreaterEqual(len(tape.close), 1)
        self.assertEqual(tape.high[-1], 105.0)
        self.assertEqual(tape.low[-1], 100.0)

    def test_rank_picks_quiet_winner_vs_btc(self) -> None:
        cfg = ForgeConfig(min_bars=90, pit_n=4, top_k=2, quiet_pct=0.5)
        eng = ForgeEngine(cfg)
        # BTC mild uptrend + small noise
        for i in range(120):
            noise = 0.15 if i % 3 == 0 else -0.05
            px = 100.0 + i * 0.05 + noise
            eng.observe_bar("BTC", i, px, px + 0.2, px - 0.2, px, 1e6)
            # QUIET winner: smooth, beats BTC
            q = 20.0 + i * 0.25
            eng.observe_bar("QET", i, q, q + 0.02, q - 0.02, q, 8e5)
            # NOISY: huge bars
            n = 30.0 + i * 0.25 + (4.0 if i % 2 == 0 else -4.0)
            eng.observe_bar("NOI", i, n, n + 5, n - 5, n, 8e5)
            # LAGGARD quiet but weak
            l = 40.0 + i * 0.01
            eng.observe_bar("LAG", i, l, l + 0.02, l - 0.02, l, 8e5)
        ranked = eng.rank(limit=4)
        picked = [s.symbol for s in ranked if s.picked]
        self.assertIn("QET", picked)
        self.assertNotIn("BTC", picked)  # BTC is the benchmark, residual ~0


if __name__ == "__main__":
    unittest.main()
