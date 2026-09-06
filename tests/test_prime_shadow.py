from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from cryptopilot.config import Settings
from cryptopilot.models import Candle, Side
from cryptopilot.prime_shadow import PrimeShadowTracker
from cryptopilot.storage import SignalStore


def _bar(open_ms: int, *, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        open_time_ms=open_ms,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1_000,
        turnover=100_000,
    )


def test_prime_shadow_records_deduplicates_and_resolves_tp2(tmp_path) -> None:
    async def check() -> None:
        store = SignalStore(tmp_path / "shadow.sqlite3")
        await store.initialize()
        created = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=1)
        entry_expires = created + timedelta(minutes=90)

        first = await store.record_prime_shadow(
            symbol="TESTUSDT",
            exchange="BYBIT",
            side=Side.LONG,
            score=90,
            created_at=created,
            entry_low=99.5,
            entry_high=100.0,
            stop_loss=98.5,
            take_profit=103.0,
            entry_expires_at=entry_expires,
            max_holding_hours=72,
            dedup_minutes=60,
        )
        duplicate = await store.record_prime_shadow(
            symbol="TESTUSDT",
            exchange="BYBIT",
            side=Side.LONG,
            score=92,
            created_at=created + timedelta(minutes=10),
            entry_low=99.6,
            entry_high=100.1,
            stop_loss=98.6,
            take_profit=103.1,
            entry_expires_at=entry_expires,
            max_holding_hours=72,
            dedup_minutes=60,
        )
        assert first > 0
        assert duplicate == 0

        interval_ms = 15 * 60 * 1000
        first_bar = (
            (int(created.timestamp() * 1000) + interval_ms - 1)
            // interval_ms
            * interval_ms
        )

        class Exchange:
            name = "BYBIT"

            async def candles(self, symbol, interval, limit):
                return [
                    _bar(first_bar, open_=99.8, high=100.2, low=99.4, close=100.0),
                    _bar(
                        first_bar + interval_ms,
                        open_=100.0,
                        high=103.2,
                        low=99.9,
                        close=102.8,
                    ),
                ]

        settings = Settings(
            _env_file=None,
            telegram_bot_token="test",
            telegram_chat_id="1",
        )
        tracker = PrimeShadowTracker(Exchange(), store, settings)
        reviewed, entered, resolved = await tracker.cycle()
        stats = await store.prime_shadow_stats()

        assert reviewed == 1
        assert entered == 0 or entered == 1
        assert resolved == 1
        assert stats["sample_size"] == 1
        assert stats["wins"] == 1
        assert stats["expectancy_r"] is not None
        assert stats["expectancy_r"] > 0

    asyncio.run(check())


def test_prime_shadow_no_entry_expires_without_counting_as_loss(tmp_path) -> None:
    async def check() -> None:
        store = SignalStore(tmp_path / "noentry.sqlite3")
        await store.initialize()
        created = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=3)
        entry_expires = created + timedelta(minutes=30)
        await store.record_prime_shadow(
            symbol="TESTUSDT",
            exchange="BYBIT",
            side=Side.SHORT,
            score=80,
            created_at=created,
            entry_low=100.0,
            entry_high=100.5,
            stop_loss=101.5,
            take_profit=97.0,
            entry_expires_at=entry_expires,
            max_holding_hours=72,
            dedup_minutes=60,
        )
        interval_ms = 15 * 60 * 1000
        first_bar = (
            (int(created.timestamp() * 1000) + interval_ms - 1)
            // interval_ms
            * interval_ms
        )

        class Exchange:
            name = "BYBIT"

            async def candles(self, symbol, interval, limit):
                return [
                    _bar(first_bar, open_=102.0, high=102.5, low=101.8, close=102.2),
                    _bar(
                        first_bar + interval_ms,
                        open_=102.2,
                        high=102.6,
                        low=101.9,
                        close=102.3,
                    ),
                    _bar(
                        first_bar + 2 * interval_ms,
                        open_=102.3,
                        high=102.7,
                        low=102.0,
                        close=102.4,
                    ),
                ]

        settings = Settings(
            _env_file=None,
            telegram_bot_token="test",
            telegram_chat_id="1",
        )
        tracker = PrimeShadowTracker(Exchange(), store, settings)
        await tracker.cycle()
        stats = await store.prime_shadow_stats()

        assert stats["no_entry"] == 1
        assert stats["sample_size"] == 0

    asyncio.run(check())
