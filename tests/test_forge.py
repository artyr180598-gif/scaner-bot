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

    def test_fliphold_keeps_after_leaving_topk(self) -> None:
        """Держим имя после входа, даже если оно выпало из топа."""
        cfg = ForgeConfig(min_bars=90, pit_n=4, top_k=1, quiet_pct=1.0)
        eng = ForgeEngine(cfg)
        for i in range(100):
            btc = 100.0 + i * 0.02
            eng.observe_bar("BTC", i, btc, btc + 0.1, btc - 0.1, btc, 1e6)
            a = 10.0 + i * 0.08
            eng.observe_bar("AAA", i, a, a + 0.05, a - 0.05, a, 9e5)
            c = 10.0 + i * 0.08
            eng.observe_bar("CCC", i, c, c + 0.05, c - 0.05, c, 9e5)
        eng._held = {"AAA"}
        eng._prev_chan = {"AAA": True, "BTC": True, "CCC": True}
        for j in range(12):
            i = 100 + j
            btc = 100.0 + i * 0.02
            eng.observe_bar("BTC", i, btc, btc + 0.1, btc - 0.1, btc, 1e6)
            a = 10.0 + 100 * 0.08 + j * 0.01
            eng.observe_bar("AAA", i, a, a + 0.05, a - 0.05, a, 9e5)
            c = 10.0 + 100 * 0.08 + j * 0.50
            eng.observe_bar("CCC", i, c, c + 0.05, c - 0.05, c, 9e5)
        later = eng.rank(limit=4)
        picked = [s.symbol for s in later if s.picked]
        self.assertIn("AAA", picked)

    def test_hydrate_and_bootstrap_no_fake_entry(self) -> None:
        cfg = ForgeConfig(min_bars=90, pit_n=4, top_k=2, quiet_pct=1.0)
        eng = ForgeEngine(cfg)
        rows_btc, rows_a = [], []
        for i in range(120):
            px = 100.0 + i * 0.05
            rows_btc.append((float(i), px, px + 0.1, px - 0.1, px, 1e6))
            q = 20.0 + i * 0.2
            rows_a.append((float(i), q, q + 0.05, q - 0.05, q, 8e5))
        self.assertGreaterEqual(eng.hydrate_bars("BTC", rows_btc), 90)
        eng.hydrate_bars("AAA", rows_a)
        eng.hydrate_bars("BBB", [(i, 15+i*0.01, 15, 15, 15+i*0.01, 8e5) for i in range(120)])
        eng.bootstrap()
        ranked = eng.rank(limit=4)
        entries = [s.symbol for s in ranked if s.entry]
        self.assertEqual(entries, [])  # после bootstrap нет ложного «ВХОД»

    def test_why_lines_wait(self) -> None:
        from forge import ForgeSnapshot
        snap = ForgeSnapshot(symbol="X", n_bars=100, liquid=False)
        self.assertEqual(snap.verdict(), "ЖДАТЬ")
        self.assertTrue(any("ликвид" in w for w in snap.why_lines()))


if __name__ == "__main__":
    unittest.main()
