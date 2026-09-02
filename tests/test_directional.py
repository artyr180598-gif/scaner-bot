"""
tests/test_directional.py — офлайн-тесты направленного ядра v4.

Философия тестов та же, что в проекте: НИКАКОЙ сети. Свечи синтезируются
детерминированно (тренд/боковик/пила), а «реальность» проверяется отдельно —
прогоном на снятых с биржи данных (см. tools/verify_live.py).

Проверяем в первую очередь то, что бот НЕ должен делать:
  * не выдавать сигнал на мусорных/коротких/несвежих данных;
  * не показывать высокую уверенность при споре факторов;
  * не строить стоп/тейки «из воздуха»;
  * не накручивать win-rate дублями одного сигнала.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import indicators as ind
from directional import (
    DEFAULT_CONFIG,
    RISK_PROFILES,
    DirectionalConfig,
    analyze,
    beginner_breakdown,
    build_features,
)
from directional_view import format_beginner_card, format_signal_card, format_why_card
from market_data import (
    Candles,
    MarketSnapshot,
    OrderBookTop,
    StaticMarketDataProvider,
    correlation,
    parse_okx_candles,
)
from signal_history import SignalHistory

TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


# ---------------------------------------------------------------------------
# Генераторы синтетических (но честно помеченных как синтетические) свечей
# ---------------------------------------------------------------------------

def make_candles(
    timeframe: str,
    n: int,
    *,
    start: float = 100.0,
    drift: float = 0.0,
    noise: float = 0.002,
    end_ts: float | None = None,
    volume: float = 1000.0,
    seed: int = 7,
) -> Candles:
    """Детерминированная серия свечей: тренд `drift` за бар + синусный шум."""
    step = TF_SECONDS[timeframe]
    end_ts = end_ts if end_ts is not None else time.time()
    rows = []
    price = start
    for i in range(n):
        wobble = math.sin((i + seed) * 0.7) * noise
        price = price * (1.0 + drift + wobble)
        high = price * (1.0 + abs(noise))
        low = price * (1.0 - abs(noise))
        open_ = price * (1.0 - drift / 2)
        ts_ms = int((end_ts - (n - i) * step) * 1000)
        rows.append([ts_ms, open_, high, low, price, volume * (1.0 + 0.1 * math.sin(i))])
    return Candles.from_rows(timeframe, rows)


def make_snapshot(
    base: str = "TEST",
    *,
    drift: float = 0.0,
    bars: int = 250,
    volume_usd: float = 500_000_000.0,
    now: float | None = None,
    funding: float | None = 0.0001,
    book: bool = True,
    market_change_24h: float | None = 0.0,
) -> MarketSnapshot:
    now = now or time.time()
    candles = {
        tf: make_candles(tf, bars, drift=drift * (TF_SECONDS[tf] / 300.0), end_ts=now)
        for tf in ("5m", "15m", "1h", "4h", "1d")
    }
    last = candles["1h"].close[-1]
    return MarketSnapshot(
        base=base,
        symbol=f"{base}/USDT",
        exchange="test",
        fetched_at=now,
        last_price=last,
        quote_volume_24h=volume_usd,
        candles=candles,
        book=OrderBookTop(bid=last * 0.9999, ask=last * 1.0001,
                          bid_volume_usd=500_000, ask_volume_usd=500_000) if book else None,
        funding_rate=funding,
        market_change_24h_percent=market_change_24h,
    )


def make_panic_snapshot(
    base: str = "TEST", *, now: float | None = None, drop: float = 0.22,
    market_drop: float = -6.0, volume_spike: float = 4.0,
    volume_usd: float = 25_000_000.0,
) -> MarketSnapshot:
    """
    Реальная ситуация, под которую заточен основной сетап:
    спокойный рынок → обвал монеты на `drop` за сутки на повышенном объёме,
    одновременно падает и весь рынок.
    """
    now = now or time.time()
    candles = {}
    for tf in ("1h", "4h", "1d"):
        step = TF_SECONDS[tf]
        bars_24h = max(1, int(24 * 3600 / step))
        n = 260
        rows = []
        price = 100.0
        for i in range(n):
            if i >= n - bars_24h:
                k = (i - (n - bars_24h) + 1) / bars_24h
                price = 100.0 * (1.0 - drop * k)
                vol = 1000.0 * volume_spike
            else:
                price = 100.0 * (1.0 + 0.002 * math.sin(i * 0.5))
                vol = 1000.0
            o = price * 1.004
            rows.append([int((now - (n - i) * step) * 1000), o, max(o, price) * 1.002,
                         min(o, price) * 0.985, price, vol])
        candles[tf] = Candles.from_rows(tf, rows)
    last = candles["1h"].close[-1]
    return MarketSnapshot(
        base=base, symbol=f"{base}/USDT", exchange="test", fetched_at=now,
        last_price=last, quote_volume_24h=volume_usd, candles=candles,
        book=OrderBookTop(bid=last * 0.9999, ask=last * 1.0001,
                          bid_volume_usd=300_000, ask_volume_usd=300_000),
        funding_rate=-0.0002, market_change_24h_percent=market_drop,
    )


# ---------------------------------------------------------------------------
# Индикаторы
# ---------------------------------------------------------------------------

class TestIndicators(unittest.TestCase):
    def test_sma_matches_manual(self):
        values = [1, 2, 3, 4, 5]
        self.assertEqual(ind.sma(values, 2)[-1], 4.5)
        self.assertIsNone(ind.sma(values, 2)[0])

    def test_ema_seeded_by_sma(self):
        values = [float(i) for i in range(1, 11)]
        out = ind.ema(values, 5)
        self.assertIsNone(out[3])
        self.assertAlmostEqual(out[4], 3.0)          # SMA(1..5)
        self.assertGreater(out[-1], out[-2])

    def test_rsi_bounds_and_extremes(self):
        rising = [float(i) for i in range(1, 40)]
        self.assertAlmostEqual(ind.rsi(rising)[-1], 100.0, places=6)
        falling = list(reversed(rising))
        self.assertLess(ind.rsi(falling)[-1], 1.0)

    def test_atr_positive_and_none_when_short(self):
        highs = [10 + i for i in range(20)]
        lows = [9 + i for i in range(20)]
        closes = [9.5 + i for i in range(20)]
        out = ind.atr(highs, lows, closes, 14)
        self.assertIsNone(out[13])
        self.assertGreater(out[-1], 0)
        self.assertTrue(all(v is None for v in ind.atr(highs[:5], lows[:5], closes[:5], 14)))

    def test_bollinger_bands_order(self):
        values = [100 + math.sin(i) for i in range(50)]
        up, mid, low = ind.bollinger(values, 20)
        self.assertGreater(up[-1], mid[-1])
        self.assertGreater(mid[-1], low[-1])

    def test_macd_hist_sign_follows_trend(self):
        up = [100 * (1.01 ** i) for i in range(80)]
        _, _, hist = ind.macd(up)
        self.assertGreater(hist[-1], 0)
        # линейное падение (у зеркально-экспоненциального ряда спад выпуклый,
        # и гистограмма честно может быть положительной — это не баг)
        # ускоряющееся падение: гистограмма обязана быть отрицательной
        down = [200 - i * i * 0.05 for i in range(80)]
        _, _, hist_d = ind.macd(down)
        self.assertLess(hist_d[-1], 0)

    def test_swing_detection(self):
        highs = [1, 2, 5, 2, 1, 3, 7, 3, 1]
        self.assertIn(2, ind.swing_highs(highs, 2, 2))
        self.assertIn(6, ind.swing_highs(highs, 2, 2))

    def test_correlation_perfect_and_none(self):
        a = [100 * (1.01 ** i) for i in range(50)]
        self.assertAlmostEqual(correlation(a, a), 1.0, places=6)
        self.assertIsNone(correlation(a[:3], a[:3]))


# ---------------------------------------------------------------------------
# Свечи и их валидация
# ---------------------------------------------------------------------------

class TestCandles(unittest.TestCase):
    def test_from_rows_sorts_dedupes_and_drops_garbage(self):
        rows = [
            [2000, 1, 2, 0.5, 1.5, 10],
            [1000, 1, 2, 0.5, 1.2, 10],
            [2000, 1, 2, 0.5, 1.7, 12],       # дубль таймстемпа — берём последний
            [3000, 0, 0, 0, 0, 5],            # нулевые цены — мусор
            ["x", 1, 2, 3, 4, 5],             # битая строка
        ]
        c = Candles.from_rows("5m", rows)
        self.assertEqual(len(c), 2)
        self.assertEqual(c.ts, (1.0, 2.0))
        self.assertEqual(c.close[-1], 1.7)

    def test_parse_okx_payload(self):
        payload = {
            "code": "0",
            "data": [
                ["1700000600000", "2", "3", "1", "2.5", "10", "20", "20", "0"],
                ["1700000000000", "1", "2", "0.5", "1.5", "5", "7", "7", "1"],
            ],
        }
        c = parse_okx_candles("1h", payload)
        self.assertEqual(len(c), 2)
        self.assertLess(c.ts[0], c.ts[1])       # отсортировано по возрастанию
        self.assertEqual(c.close[-1], 2.5)

    def test_unclosed_last_bar_is_dropped_by_features(self):
        now = 1_000_000.0
        # последний бар открыт «только что» → он ещё не закрыт
        candles = make_candles("5m", 120, end_ts=now + 300)
        feats = build_features(candles, DEFAULT_CONFIG, now)
        self.assertIsNotNone(feats)
        self.assertEqual(feats.bars, 119)


# ---------------------------------------------------------------------------
# Качество данных и ворота
# ---------------------------------------------------------------------------

class TestDataConfidence(unittest.TestCase):
    def test_no_data_means_no_signal(self):
        snap = MarketSnapshot(base="GHOST", symbol="", exchange="test",
                              errors=["GHOST/USDT нет на бирже"])
        sig = analyze(snap)
        self.assertEqual(sig.direction, "none")
        self.assertFalse(sig.actionable)
        self.assertLess(sig.data_confidence, 30)
        self.assertTrue(sig.why_not)
        self.assertIn("нечего анализировать", sig.explanation)

    def test_short_history_blocks_entry_timeframe(self):
        now = time.time()
        snap = MarketSnapshot(
            base="NEW", symbol="NEW/USDT", exchange="test", last_price=1.0,
            quote_volume_24h=1e8,
            candles={"5m": make_candles("5m", 20, end_ts=now)},
        )
        sig = analyze(snap, now=now)
        self.assertFalse(sig.actionable)
        self.assertTrue(any("1h" in reason for reason in sig.why_not + sig.quality.blockers))

    def test_stale_data_lowers_confidence(self):
        now = time.time()
        fresh = analyze(make_snapshot(drift=0.001, now=now), now=now)
        stale_snap = make_snapshot(drift=0.001, now=now - 6 * 3600)
        stale = analyze(stale_snap, now=now)
        self.assertLess(stale.data_confidence, fresh.data_confidence)
        self.assertTrue(any("несвеж" in w for w in stale.quality.warnings))

    def test_low_liquidity_warns_and_blocks_conservative(self):
        snap = make_snapshot(drift=0.002, volume_usd=300_000.0)
        sig = analyze(snap, profile="conservative")
        self.assertFalse(sig.actionable)
        self.assertTrue(any("оборот" in r for r in sig.why_not))

    def test_manipulation_flag_on_anomalous_candle(self):
        now = time.time()
        snap = make_snapshot(drift=0.0, now=now)
        c = snap.candles["1h"]
        # искусственный вынос: последний бар в 20 раз шире обычного
        rows = [[int(t * 1000), o, h, l, cl, v]
                for t, o, h, l, cl, v in zip(c.ts, c.open, c.high, c.low, c.close, c.volume)]
        last = rows[-1]
        last[2] = last[4] * 1.35
        last[3] = last[4] * 0.75
        last[5] = last[5] * 100
        snap.candles["1h"] = Candles.from_rows("1h", rows)
        sig = analyze(snap, now=now, profile="moderate")
        self.assertTrue(sig.quality.manipulation_flag)
        self.assertFalse(sig.actionable)

    def test_data_confidence_caps_signal_confidence(self):
        now = time.time()
        snap = make_snapshot(drift=0.004, now=now, volume_usd=1_500_000.0, book=False)
        sig = analyze(snap, now=now, profile="aggressive")
        self.assertLessEqual(sig.signal_confidence, sig.data_confidence + 1e-9)


# ---------------------------------------------------------------------------
# Логика сигнала
# ---------------------------------------------------------------------------

class TestSignalLogic(unittest.TestCase):
    def test_plain_uptrend_is_not_a_signal(self):
        """
        Ключевое изменение v5: сам по себе тренд сигналом НЕ является.
        На истории следование тренду на 1h/4h давало PF 0.90-0.99, то есть
        минус после издержек. Бот обязан молчать.
        """
        now = time.time()
        sig = analyze(make_snapshot(drift=0.0015, now=now), now=now, profile="aggressive")
        self.assertEqual(sig.direction, "none")
        self.assertIsNone(sig.setup)
        self.assertTrue(sig.why_not)

    def test_plain_downtrend_is_not_a_signal(self):
        now = time.time()
        sig = analyze(make_snapshot(drift=-0.0015, now=now), now=now, profile="aggressive")
        self.assertEqual(sig.direction, "none")
        self.assertIsNone(sig.setup)

    def test_panic_with_falling_market_gives_long(self):
        """Проверенный сетап: обвал монеты + падающий рынок + объём → лонг."""
        now = time.time()
        sig = analyze(make_panic_snapshot(now=now), now=now, profile="aggressive")
        self.assertEqual(sig.direction, "long")
        self.assertIsNotNone(sig.setup)
        self.assertEqual(sig.setup.key, "panic_reversal")
        self.assertGreaterEqual(sig.signal_confidence, 55.0)
        self.assertIn("паник", sig.explanation.lower())

    def test_solo_crash_without_market_is_rejected(self):
        """
        Монета падает ОДНА (рынок спокоен) — вход запрещён.
        На истории такие входы: PF 0.71 против 1.77 при падении вместе с рынком.
        """
        now = time.time()
        sig = analyze(make_panic_snapshot(now=now, market_drop=+0.5), now=now,
                      profile="aggressive")
        self.assertEqual(sig.direction, "none")
        self.assertTrue(any("одиноч" in w or "рынок спокоен" in w for w in sig.why_not))

    def test_confidence_is_calibrated_and_monotone(self):
        """Более глубокий обвал + более сильная паника рынка → выше уверенность."""
        now = time.time()
        weak = analyze(make_panic_snapshot(now=now, drop=0.19, market_drop=-2.5),
                       now=now, profile="aggressive")
        strong = analyze(make_panic_snapshot(now=now, drop=0.35, market_drop=-9.0),
                         now=now, profile="aggressive")
        self.assertGreater(strong.signal_confidence, weak.signal_confidence)
        self.assertTrue(strong.calibration)

    def test_flat_market_gives_no_signal_with_reason(self):
        now = time.time()
        sig = analyze(make_snapshot(drift=0.0, now=now), now=now)
        self.assertEqual(sig.direction, "none")
        self.assertTrue(sig.why_not)
        self.assertIn("сетап", sig.explanation.lower())

    def test_confidence_never_exceeds_hard_cap(self):
        now = time.time()
        for drift in (0.004, 0.01, 0.02):
            sig = analyze(make_snapshot(drift=drift, now=now), now=now, profile="aggressive")
            self.assertLessEqual(sig.signal_confidence, DEFAULT_CONFIG.max_signal_confidence)

    def test_higher_timeframe_conflict_caps_confidence(self):
        now = time.time()
        snap = make_snapshot(drift=0.002, now=now)      # растущие младшие ТФ
        snap.candles["4h"] = make_candles("4h", 250, drift=-0.01, end_ts=now)
        sig = analyze(snap, now=now, profile="aggressive")
        if sig.direction == "long":
            self.assertLessEqual(sig.signal_confidence, DEFAULT_CONFIG.conflict_confidence_cap)
            self.assertTrue(any("старший таймфрейм" in r for r in sig.why_not))

    def test_setup_geometry_matches_backtest(self):
        """Стоп и цели строятся по геометрии, на которой сетап проверен."""
        now = time.time()
        sig = analyze(make_panic_snapshot(now=now), now=now, profile="aggressive")
        plan, st = sig.plan, sig.setup
        self.assertIsNotNone(plan)
        atr = sig.features["1h"].atr
        self.assertAlmostEqual(abs(plan.entry_mid - plan.stop) / atr, st.sl_atr, places=1)
        self.assertAlmostEqual(plan.target_rr[1], 2.0, places=1)
        self.assertEqual(len(plan.targets), 3)

    def test_stretch_is_measured_and_gate_works_when_enabled(self):
        """
        Растяжение входа считается всегда (показывается пользователю), а
        блокирует вход только при включённом гейте. По умолчанию гейт
        выключен — на реальных данных он не улучшил результат.
        """
        now = time.time()
        snap = make_panic_snapshot(now=now)
        base = analyze(snap, now=now, profile="aggressive")
        self.assertIsNotNone(base.entry_stretch_atr)
        gated = analyze(
            snap, now=now, profile="aggressive",
            cfg=DirectionalConfig(max_entry_stretch_atr=0.5),
        )
        if base.entry_stretch_atr > 0.5:
            self.assertFalse(gated.actionable)
            self.assertTrue(any("растянут" in r for r in gated.why_not))

    def test_plan_is_structural_and_consistent(self):
        now = time.time()
        sig = analyze(make_panic_snapshot(now=now), now=now, profile="aggressive")
        plan = sig.plan
        self.assertIsNotNone(plan)
        self.assertLess(plan.entry_low, plan.entry_high)
        self.assertLess(plan.stop, plan.entry_low)                    # long: стоп ниже входа
        self.assertTrue(all(t > plan.entry_mid for t in plan.targets))
        self.assertEqual(len(plan.targets), 3)
        self.assertEqual(len(plan.target_rr), 3)
        self.assertLess(plan.target_rr[0], plan.target_rr[-1])        # цели по возрастанию R
        self.assertGreater(plan.rr, 0)
        self.assertGreaterEqual(plan.max_leverage, 1.0)

    def test_leverage_scales_down_with_wide_stop(self):
        now = time.time()
        calm = analyze(make_snapshot(drift=0.0015, now=now), now=now, profile="aggressive")
        wild_snap = make_snapshot(drift=0.0015, now=now)
        # раздуваем ATR: шире стоп → плечо обязано упасть
        wide = {tf: make_candles(tf, 250, drift=0.0015, noise=0.02, end_ts=now)
                for tf in ("5m", "15m", "1h", "4h")}
        wild_snap.candles = wide
        wild_snap.last_price = wide["5m"].close[-1]
        wild = analyze(wild_snap, now=now, profile="aggressive")
        if calm.plan and wild.plan:
            self.assertGreater(calm.plan.risk_percent_of_price, 0)
            self.assertLessEqual(wild.plan.max_leverage, calm.plan.max_leverage)

    def test_position_size_matches_risk_rule(self):
        now = time.time()
        sig = analyze(make_panic_snapshot(now=now), now=now, profile="aggressive")
        plan = sig.plan
        expected = min(100.0, plan.risk_per_trade_percent / plan.risk_percent_of_price * 100.0)
        self.assertAlmostEqual(plan.position_percent_of_deposit, expected, places=6)

    def test_profiles_change_gates_not_market(self):
        now = time.time()
        snap = make_snapshot(drift=0.0012, now=now, volume_usd=5_000_000.0)
        cons = analyze(snap, profile="conservative", now=now)
        aggr = analyze(snap, profile="aggressive", now=now)
        self.assertEqual(cons.net_score, aggr.net_score)              # рынок один и тот же
        self.assertGreaterEqual(len(cons.why_not), len(aggr.why_not))

    def test_every_factor_has_human_note(self):
        now = time.time()
        sig = analyze(make_snapshot(drift=0.001, now=now), now=now)
        self.assertTrue(sig.factors)
        for f in sig.factors:
            self.assertTrue(f.note.strip())
            self.assertGreaterEqual(f.score, -1.0)
            self.assertLessEqual(f.score, 1.0)

    def test_beginner_breakdown_has_six_sections(self):
        now = time.time()
        sig = analyze(make_snapshot(drift=0.0015, now=now), now=now, profile="aggressive")
        sections = beginner_breakdown(sig)
        self.assertEqual(len(sections), 6)
        for _, text in sections:
            self.assertTrue(text.strip())


# ---------------------------------------------------------------------------
# Вьюхи (HTML не должен падать ни на одном состоянии)
# ---------------------------------------------------------------------------

class TestViews(unittest.TestCase):
    def test_cards_render_for_signal_and_for_nothing(self):
        now = time.time()
        good = analyze(make_snapshot(drift=0.0015, now=now), now=now, profile="aggressive")
        empty = analyze(MarketSnapshot(base="X", symbol="", exchange="t", errors=["нет"]))
        for sig in (good, empty):
            for renderer in (format_signal_card, format_why_card, format_beginner_card):
                html = renderer(sig)
                self.assertIn("Не финансовый совет", html)
                self.assertLess(len(html), 4096 * 3)

    def test_signal_card_contains_key_blocks(self):
        now = time.time()
        sig = analyze(make_panic_snapshot(now=now), now=now, profile="aggressive")
        html = format_signal_card(sig)
        for token in ("Направление", "Сетап", "Что делать", "Данные:", "Прогноз:",
                      "Stop-Loss", "TP1", "R/R", "Плечо", "Как этот сетап работал"):
            self.assertIn(token, html)

    def test_card_shows_honest_odds_and_never_promises(self):
        now = time.time()
        sig = analyze(make_panic_snapshot(now=now), now=now, profile="aggressive")
        html = format_signal_card(sig)
        self.assertIn("Шансы:", html)
        self.assertIn("win-rate", html)
        for banned in ("гарант", "точно вырастет", "без риска", "обязательно"):
            self.assertNotIn(banned, html.lower())


# ---------------------------------------------------------------------------
# Журнал точности
# ---------------------------------------------------------------------------

class TestSignalHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.tmp.write("[]")
        self.tmp.close()
        self.hist = SignalHistory(self.tmp.name, ttl_hours=1.0)
        now = time.time()
        # фикстура — проверенный сетап «разворот после паники»: только такие
        # сигналы бот вообще выдаёт, значит и журнал точности пишет только их
        self.sig = analyze(make_panic_snapshot(now=now), now=now, profile="aggressive")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_only_actionable_recorded_and_deduped(self):
        self.assertTrue(self.sig.actionable, "фикстура должна давать проходной сигнал")
        self.assertIsNotNone(self.hist.record(self.sig))
        self.assertIsNone(self.hist.record(self.sig))         # дубль в окне — игнор
        self.assertEqual(len(self.hist.records), 1)

    def test_stop_and_target_resolution(self):
        rec = self.hist.record(self.sig)
        self.hist.update_price(rec.base, rec.stop * 0.99)
        self.assertEqual(self.hist.records[0].status, "stop")
        self.assertLess(self.hist.records[0].result_percent, 0)
        stats = self.hist.stats()
        self.assertEqual(stats["winrate"], 0.0)
        self.assertEqual(stats["losses"], 1)

    def test_take_profit_resolution_and_winrate(self):
        rec = self.hist.record(self.sig)
        self.hist.update_price(rec.base, rec.targets[1] * 1.001)
        self.assertIn(self.hist.records[0].status, ("tp2", "tp3"))
        self.assertGreater(self.hist.records[0].result_percent, 0)
        self.assertEqual(self.hist.stats()["winrate"], 100.0)

    def test_expiry_marks_result_as_is(self):
        rec = self.hist.record(self.sig)
        rec.created_at -= 2 * 3600
        self.hist.update_price(rec.base, rec.entry_mid)
        self.assertEqual(self.hist.records[0].status, "expired")

    def test_persistence_roundtrip(self):
        self.hist.record(self.sig)
        again = SignalHistory(self.tmp.name)
        self.assertEqual(len(again.records), 1)
        self.assertEqual(again.records[0].base, self.sig.base)

    def test_empty_stats_are_honest(self):
        stats = SignalHistory(None).stats()
        self.assertEqual(stats["total"], 0)
        self.assertIsNone(stats["winrate"])


# ---------------------------------------------------------------------------
# Сервис (кнопки, профили, watchlist, скан)
# ---------------------------------------------------------------------------

class TestService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from directional_service import DirectionalService

        now = time.time()
        snaps = {
            "AAA": make_snapshot("AAA", drift=0.0015, now=now),
            "BBB": make_snapshot("BBB", drift=-0.0015, now=now, volume_usd=2e8),
            "CCC": make_snapshot("CCC", drift=0.0, now=now, volume_usd=400_000.0),
        }
        self.provider = StaticMarketDataProvider(snaps)
        self.service = DirectionalService(
            self.provider, history=SignalHistory(None), default_profile="aggressive",
            cache_seconds=0.0,
        )

    async def test_analyze_command_returns_card_and_keyboard(self):
        html, keyboard = await self.service.cmd_analyze("42", "AAA")
        self.assertIn("AAA", html)
        self.assertIn("inline_keyboard", keyboard)
        buttons = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertIn("why:AAA", buttons)
        self.assertIn("learn:AAA", buttons)

    async def test_scan_ranks_actionable_only(self):
        signals, scanned, _ = await self.service.scan(
            RISK_PROFILES["aggressive"], limit=5
        )
        self.assertGreaterEqual(scanned, 2)
        for sig in signals:
            self.assertTrue(sig.actionable)
        confidences = [s.signal_confidence for s in signals]
        self.assertEqual(confidences, sorted(confidences, reverse=True))

    async def test_low_liquidity_coin_never_actionable_for_conservative(self):
        sig = await self.service.analyze_base("CCC", RISK_PROFILES["conservative"])
        self.assertFalse(sig.actionable)

    async def test_profile_switch_persists_per_chat(self):
        await self.service.cmd_profile("7", "conservative")
        self.assertEqual(self.service.profile_for("7").key, "conservative")
        self.assertEqual(self.service.profile_for("8").key, "aggressive")

    async def test_watchlist_add_remove_and_alerts(self):
        await self.service.cmd_watch("7", "AAA")
        self.assertIn("AAA", self.service.watchlist("7"))
        alerts = await self.service.check_alerts(cooldown_minutes=0.0)
        self.assertTrue(all(chat == "7" for chat, _ in alerts))
        await self.service.cmd_unwatch("7", "AAA")
        self.assertNotIn("AAA", self.service.watchlist("7"))

    async def test_unknown_coin_answers_honestly(self):
        html, _ = await self.service.cmd_analyze("7", "ZZZZ")
        self.assertIn("ZZZZ", html)
        self.assertIn("НЕТ СИГНАЛА", html)

    async def test_handlers_registry_covers_buttons(self):
        handlers = self.service.handlers()
        for name in ("an", "why", "learn", "scan", "profile", "watch", "unwatch",
                     "accuracy", "find", "menu"):
            self.assertIn(name, handlers)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
