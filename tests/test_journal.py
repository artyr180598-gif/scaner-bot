"""
tests/test_journal.py — журнал сигналов: исходы, статистика, калибровка.

Журнал — единственное место, где бот отвечает за свои слова. Проверяем
консервативность исходов (стоп приоритетнее цели на том же баре), корректность
статистики и устойчивость к битому файлу.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config.settings import Settings
from app.data.synthetic import make_snapshot
from app.domain.models import (Candles, Direction, MarketContext, Target,
                               Timeframe, TradePlan)
from app.services.journal import (OUTCOME_EXPIRED, OUTCOME_STOP, OUTCOME_TP1,
                                  OUTCOME_TP3, JournalEntry, SignalJournal,
                                  _calibration_buckets)

H1_MS = 3_600_000


def _plan(direction=Direction.LONG) -> TradePlan:
    long_ = direction is Direction.LONG
    entry = 100.0
    stop = 95.0 if long_ else 105.0
    targets = [105.0, 110.0, 115.0] if long_ else [95.0, 90.0, 85.0]
    return TradePlan(direction=direction, entry_low=entry, entry_high=entry, stop=stop,
                     targets=[Target(price, f"TP{n}", fraction=0.35)
                              for n, price in enumerate(targets, 1)])


def _signal(direction=Direction.LONG, symbol="AAA/USDT", confidence=7.0,
            horizon=24, created_at=None) -> "object":
    from app.analysis.base import Group
    from app.domain.models import Factor, FactorSet, Signal

    fs = FactorSet(factors=[
        Factor("t", Group.TREND, 0.8, "тренд вверх"),
        Factor("s", Group.STRUCTURE, 0.7, "пробой структуры"),
    ])
    return Signal(symbol=symbol, direction=direction, confidence=confidence, score=0.6,
                  timeframe=Timeframe.H1, plan=_plan(direction), factors=fs,
                  summary="тестовый сигнал", setup="сжатие + спрос", price=100.0,
                  horizon_hours=horizon,
                  created_at=created_at or datetime.now(timezone.utc))


class FakeMarket:
    """Мини-шлюз данных: отдаёт заранее подготовленные свечи."""

    def __init__(self, rows):
        self._rows = rows

    async def candles(self, symbol, timeframe, limit=300):
        return Candles.from_raw(symbol, timeframe, self._rows, source="fake")


def rows(bars, start: datetime, step_ms: int = H1_MS):
    """bars: список (high, low). Close/Open — середина диапазона."""
    out = []
    base = int(start.timestamp() * 1000)
    for i, (high, low) in enumerate(bars):
        ts = base + i * step_ms
        mid = (high + low) / 2
        out.append([ts, mid, high, low, mid, 100.0])
    return out


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def journal(tmp_path) -> SignalJournal:
    return SignalJournal(tmp_path / "journal.json")


# ---------------------------------------------------------------------------
# Запись и хранение
# ---------------------------------------------------------------------------

def test_add_creates_open_entry(journal):
    run(journal.add(_signal(), potential=0.7))
    entries = journal.entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.symbol == "AAA/USDT"
    assert entry.direction == "LONG"
    assert entry.outcome == "open"
    assert entry.stop == 95.0
    assert entry.targets == [105.0, 110.0, 115.0]
    assert entry.potential == 0.7
    assert len(journal.open_entries()) == 1


def test_persisted_to_disk_and_reloaded(journal):
    run(journal.add(_signal()))
    payload = json.loads(journal.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["entries"]) == 1
    reloaded = SignalJournal(journal.path)
    assert len(reloaded.entries()) == 1


def test_corrupt_file_starts_clean(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ это не json", encoding="utf-8")
    journal = SignalJournal(path)
    assert journal.entries() == []
    # И всё равно можно писать дальше.
    run(journal.add(_signal()))
    assert len(SignalJournal(path).entries()) == 1


def test_entries_limit_and_symbol_filter(journal):
    for i in range(5):
        run(journal.add(_signal(symbol=f"S{i}/USDT")))
    assert len(journal.entries(limit=2)) == 2
    assert [e.symbol for e in journal.entries(symbol="S3/USDT")] == ["S3/USDT"]
    # Самые свежие — первыми.
    assert journal.entries(limit=1)[0].symbol == "S4/USDT"


def test_nan_serialises_as_null(journal):
    entry = JournalEntry.from_signal(_signal())
    entry.outcome_price = float("nan")
    assert entry.to_dict()["outcome_price"] is None


def test_entry_from_signal_without_plan():
    signal = _signal()
    signal.plan = None
    entry = JournalEntry.from_signal(signal)
    assert entry.targets == []
    assert entry.rr == 0.0


# ---------------------------------------------------------------------------
# Исходы
# ---------------------------------------------------------------------------

def test_stop_on_same_bar_wins_over_take_profit(journal):
    """Пессимизм: внутри барный порядок неизвестен → засчитываем стоп."""
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    run(journal.add(_signal(created_at=created)))
    market = FakeMarket(rows([(111.0, 94.0)], created + timedelta(hours=1)))
    assert run(journal.update_outcomes(market)) == 1
    entry = journal.entries()[0]
    assert entry.outcome == OUTCOME_STOP
    assert entry.r_result == -1.0
    assert journal.open_entries() == []


def test_take_profit_first_target(journal):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    run(journal.add(_signal(created_at=created)))
    market = FakeMarket(rows([(106.0, 97.0)], created + timedelta(hours=1)))
    assert run(journal.update_outcomes(market)) == 1
    entry = journal.entries()[0]
    assert entry.outcome == OUTCOME_TP1
    assert entry.r_result == 1.0
    assert entry.outcome_price == 105.0


def test_highest_target_reached_is_counted(journal):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    run(journal.add(_signal(created_at=created)))
    market = FakeMarket(rows([(116.0, 99.0)], created + timedelta(hours=1)))
    run(journal.update_outcomes(market))
    entry = journal.entries()[0]
    assert entry.outcome == OUTCOME_TP3
    assert entry.r_result == 3.0


def test_no_touch_keeps_position_open_but_tracks_extremes(journal):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    run(journal.add(_signal(created_at=created)))
    market = FakeMarket(rows([(103.0, 98.0), (104.0, 96.0)],
                             created + timedelta(hours=1)))
    assert run(journal.update_outcomes(market)) == 0
    entry = journal.entries()[0]
    assert entry.outcome == "open"
    assert entry.max_favourable_r == pytest.approx(0.8)      # (104-100)/5
    assert entry.max_adverse_r == pytest.approx(-0.8)        # (100-96)/5


def test_expired_after_horizon(journal):
    created = datetime.now(timezone.utc) - timedelta(hours=8)
    run(journal.add(_signal(created_at=created, horizon=6)))
    market = FakeMarket(rows([(103.0, 97.0)], created + timedelta(hours=1)))
    run(journal.update_outcomes(market))
    entry = journal.entries()[0]
    assert entry.outcome == OUTCOME_EXPIRED
    assert entry.r_result == 0.0


def test_short_signal_outcome(journal):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    run(journal.add(_signal(direction=Direction.SHORT, created_at=created)))
    market = FakeMarket(rows([(101.0, 94.0)], created + timedelta(hours=1)))
    run(journal.update_outcomes(market))
    entry = journal.entries()[0]
    assert entry.direction == "SHORT"
    assert entry.outcome == OUTCOME_TP1
    assert entry.r_result == 1.0


def test_short_stop(journal):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    run(journal.add(_signal(direction=Direction.SHORT, created_at=created)))
    market = FakeMarket(rows([(106.0, 99.0)], created + timedelta(hours=1)))
    run(journal.update_outcomes(market))
    assert journal.entries()[0].outcome == OUTCOME_STOP


def test_bars_before_signal_are_ignored(journal):
    """Свечи до момента сигнала не должны ничего «достигать»."""
    created = datetime.now(timezone.utc) - timedelta(hours=2)
    run(journal.add(_signal(created_at=created)))
    market = FakeMarket(rows([(120.0, 90.0)], created - timedelta(hours=5)))
    assert run(journal.update_outcomes(market)) == 0
    assert journal.entries()[0].outcome == "open"


def test_empty_market_is_safe(journal):
    run(journal.add(_signal()))
    assert run(journal.update_outcomes(FakeMarket([]))) == 0


# ---------------------------------------------------------------------------
# Кулдаун
# ---------------------------------------------------------------------------

def test_is_recent_respects_cooldown(journal):
    assert not journal.is_recent("AAA/USDT", "LONG")
    run(journal.add(_signal()))
    assert journal.is_recent("AAA/USDT", "LONG")
    assert not journal.is_recent("AAA/USDT", "SHORT")
    assert not journal.is_recent("BBB/USDT", "LONG")
    assert not journal.is_recent("AAA/USDT", "LONG", cooldown_minutes=0)


def test_is_recent_ignores_stale_entry(journal):
    old = _signal(created_at=datetime.now(timezone.utc) - timedelta(hours=5))
    run(journal.add(old))
    assert not journal.is_recent("AAA/USDT", "LONG", cooldown_minutes=120)


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def _close(journal, symbol: str, direction: Direction, outcome: str, r: float,
           confidence: float = 7.0):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    run(journal.add(_signal(symbol=symbol, direction=direction, confidence=confidence,
                            created_at=created)))
    entry = journal.entries(symbol=symbol)[0]
    entry.outcome = outcome
    entry.r_result = r


def test_stats_win_rate_and_r(journal):
    _close(journal, "A/USDT", Direction.LONG, OUTCOME_STOP, -1.0)
    _close(journal, "B/USDT", Direction.LONG, OUTCOME_TP1, 1.0)
    _close(journal, "C/USDT", Direction.LONG, OUTCOME_TP3, 3.0)
    run(journal.add(_signal(symbol="D/USDT")))          # остаётся открытым

    stats = journal.stats()
    assert stats["total"] == 4
    assert stats["closed"] == 3
    assert stats["open"] == 1
    assert stats["win_rate"] == pytest.approx(66.7)
    assert stats["stop_rate"] == pytest.approx(33.3)
    assert stats["avg_r"] == pytest.approx(1.0)
    assert stats["total_r"] == pytest.approx(3.0)
    assert len(stats["calibration"]) == 1
    assert stats["calibration"][0]["n"] == 3


def test_stats_empty_journal_has_note(journal):
    stats = journal.stats()
    assert stats["total"] == 0
    assert stats["closed"] == 0
    assert "нет" in stats["note"]
    assert "win_rate" not in stats


def test_calibration_buckets_split_by_confidence(journal):
    _close(journal, "A/USDT", Direction.LONG, OUTCOME_STOP, -1.0, confidence=3.0)
    _close(journal, "B/USDT", Direction.LONG, OUTCOME_TP1, 1.0, confidence=5.0)
    _close(journal, "C/USDT", Direction.LONG, OUTCOME_TP1, 1.0, confidence=7.0)
    _close(journal, "D/USDT", Direction.LONG, OUTCOME_STOP, -1.0, confidence=9.0)
    stats = journal.stats()
    ranges = {b["range"]: b for b in stats["calibration"]}
    assert "0–5" in ranges and "5–6.5" in ranges and "6.5–8" in ranges
    assert ranges["0–5"]["win_rate"] == 0.0
    assert ranges["5–6.5"]["win_rate"] == 100.0
    assert ranges["6.5–8"]["avg_r"] == 1.0


def test_calibration_buckets_helper_directly():
    assert _calibration_buckets([]) == []


def test_journal_used_with_real_engine_signal(journal):
    """Интеграция: сигнал из движка пишется в журнал без потерь."""
    settings = Settings()
    settings.min_confidence = 4.0
    settings.min_rr = 1.1
    from app.signals.engine import SignalEngine

    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    signal = SignalEngine(settings).analyze(snapshot, MarketContext())
    assert signal.actionable
    run(journal.add(signal, potential=0.62))
    entry = journal.entries()[0]
    assert entry.symbol == "TEST/USDT"
    assert entry.stop == pytest.approx(signal.plan.stop)
    assert entry.targets == [t.price for t in signal.plan.targets]
    assert entry.rr == pytest.approx(round(signal.rr, 2))
