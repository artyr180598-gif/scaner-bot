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

    def test_features_have_stop_and_sma(self) -> None:
        eng = ForgeEngine(ForgeConfig(min_bars=90, pit_n=4, top_k=2, quiet_pct=1.0))
        for i in range(120):
            px = 100.0 + i * 0.05
            eng.observe_bar("BTC", i, px, px + 0.2, px - 0.2, px, 1e6)
            q = 20.0 + i * 0.2
            eng.observe_bar("AAA", i, q, q + 0.05, q - 0.05, q, 8e5)
        snap = eng.snapshot("AAA")
        self.assertIsNotNone(snap.sma)
        self.assertIsNotNone(snap.stop)
        self.assertIsNotNone(snap.atr)
        self.assertGreater(snap.close, 0)

    def test_rank_keeps_entry_on_same_bar(self) -> None:
        cfg = ForgeConfig(min_bars=90, pit_n=4, top_k=2, quiet_pct=1.0)
        eng = ForgeEngine(cfg)
        for i in range(120):
            px = 100.0 + i * 0.05
            eng.observe_bar("BTC", float(i), px, px + 0.2, px - 0.2, px, 1e6)
            q = 20.0 + i * 0.25
            eng.observe_bar("AAA", float(i), q, q + 0.05, q - 0.05, q, 8e5)
            w = 15.0 + i * 0.02
            eng.observe_bar("BBB", float(i), w, w + 0.02, w - 0.02, w, 8e5)
        first = eng.rank(limit=0)
        second = eng.rank(limit=0)
        e1 = {s.symbol for s in first if s.entry}
        e2 = {s.symbol for s in second if s.entry}
        self.assertEqual(e1, e2)

    def test_buy_ok_needs_all_filters(self) -> None:
        from forge import ForgeSnapshot
        snap = ForgeSnapshot(
            symbol="SOL", n_bars=120, close=100.0, resid=0.04, vol=0.02,
            sma=90.0, stop=92.0, atr=2.0, above_sma=True, chandelier_ok=True,
            quiet=True, liquid=True, picked=True, entry=True,
        )
        self.assertTrue(snap.buy_ok())
        self.assertEqual(snap.verdict(), "ВХОД")
        snap.quiet = False
        self.assertFalse(snap.buy_ok())
        self.assertNotEqual(snap.verdict(), "ВХОД")

    def test_watch_when_waiting_chandelier(self) -> None:
        from forge import ForgeSnapshot
        snap = ForgeSnapshot(
            symbol="AAA", n_bars=120, resid=0.03, above_sma=True,
            chandelier_ok=False, quiet=True, liquid=True, setup=True,
        )
        self.assertEqual(snap.verdict(), "СМОТРЕТЬ")
        self.assertFalse(snap.buy_ok())

    def test_upsert_replaces_same_day(self) -> None:
        eng = ForgeEngine(bar_seconds=86400.0)
        rows = [(86400.0 * i, 10.0, 11.0, 9.0, 10.0 + i * 0.01, 100.0) for i in range(5)]
        eng.hydrate_bars("AAA", rows)
        n = len(eng.tape("AAA").close)
        eng.hydrate_bars("AAA", [(86400.0 * 4, 10.0, 12.0, 9.0, 11.5, 200.0)])
        self.assertEqual(len(eng.tape("AAA").close), n)
        self.assertEqual(eng.tape("AAA").close[-1], 11.5)
        self.assertEqual(eng.tape("AAA").high[-1], 12.0)


if __name__ == "__main__":
    unittest.main()
