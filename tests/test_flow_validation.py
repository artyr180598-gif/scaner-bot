from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from cryptopilot.flow_validation import FlowForwardValidator, _first_trigger_time
from cryptopilot.models import Candle, Side
from cryptopilot.storage import SignalStore


def _candle(open_ms: int, *, high: float, low: float, close: float = 100.0) -> Candle:
    return Candle(
        open_time_ms=open_ms,
        open=100.0,
        high=high,
        low=low,
        close=close,
        volume=1_000,
        turnover=100_000,
    )


def test_first_trigger_time_skips_candle_already_open_at_alert() -> None:
    base = datetime(2026, 9, 6, 10, 0, 30, tzinfo=UTC)
    created_ms = int(base.timestamp() * 1000)
    minute_open = created_ms // 60_000 * 60_000
    item = {
        "created_at": base,
        "expires_at": base + timedelta(minutes=45),
        "bias": Side.LONG,
        "trigger_price": 101.0,
    }
    candles = [
        # This wick could have happened before the alert, so it must not count.
        _candle(minute_open, high=102.0, low=99.0),
        # First fully observable minute after the alert.
        _candle(minute_open + 60_000, high=101.2, low=99.8),
    ]

    triggered = _first_trigger_time(item, candles)

    assert triggered is not None
    assert triggered == datetime.fromtimestamp((minute_open + 120_000) / 1000, UTC)


def test_flow_observation_storage_and_statistics(tmp_path) -> None:
    async def check() -> None:
        store = SignalStore(tmp_path / "flow.sqlite3")
        await store.initialize()
        now = datetime.now(UTC) - timedelta(minutes=5)
        first = await store.record_flow_observation(
            symbol="BTCUSDT",
            bias=Side.LONG,
            score=82,
            event_type="FLOW_BUILDUP",
            event_price=100.0,
            trigger_price=101.0,
            created_at=now,
            window_minutes=45,
        )
        second = await store.record_flow_observation(
            symbol="ETHUSDT",
            bias=Side.SHORT,
            score=78,
            event_type="ABSORPTION",
            event_price=50.0,
            trigger_price=49.0,
            created_at=now,
            window_minutes=45,
        )

        pending = await store.pending_flow_observations()
        assert {item["id"] for item in pending} == {first, second}

        await store.resolve_flow_observation(
            first,
            status="TRIGGERED",
            resolved_at=now + timedelta(minutes=3),
            triggered_at=now + timedelta(minutes=2),
            lead_seconds=120,
        )
        await store.resolve_flow_observation(
            second,
            status="EXPIRED",
            resolved_at=now + timedelta(minutes=45),
        )
        stats = await store.flow_validation_stats()

        assert stats["resolved"] == 2
        assert stats["triggered"] == 1
        assert stats["expired"] == 1
        assert stats["pending"] == 0
        assert stats["trigger_rate_pct"] == 50.0
        assert stats["median_lead_seconds"] == 120.0

    asyncio.run(check())


def test_forward_validator_marks_trigger_conservatively(tmp_path) -> None:
    async def check() -> None:
        store = SignalStore(tmp_path / "validator.sqlite3")
        await store.initialize()
        now = datetime.now(UTC).replace(second=30, microsecond=0)
        created = now - timedelta(minutes=3)
        created_ms = int(created.timestamp() * 1000)
        first_safe = ((created_ms // 60_000) + 1) * 60_000
        await store.record_flow_observation(
            symbol="TESTUSDT",
            bias=Side.LONG,
            score=80,
            event_type="FLOW_BUILDUP",
            event_price=99.5,
            trigger_price=100.0,
            created_at=created,
            window_minutes=45,
        )

        class Exchange:
            async def candles(self, symbol, interval, limit):
                assert symbol == "TESTUSDT"
                assert interval == "1"
                return [
                    _candle(first_safe, high=99.9, low=99.0),
                    _candle(first_safe + 60_000, high=100.2, low=99.4),
                ]

        settings = type("SettingsStub", (), {})()
        validator = FlowForwardValidator(Exchange(), store, settings)
        resolved, triggered = await validator.cycle()
        stats = await store.flow_validation_stats()

        assert (resolved, triggered) == (1, 1)
        assert stats["triggered"] == 1
        assert stats["median_lead_seconds"] is not None
        assert stats["median_lead_seconds"] > 0

    asyncio.run(check())


def test_notification_budget_blocks_second_prime_alert_same_window(tmp_path) -> None:
    async def check() -> None:
        store = SignalStore(tmp_path / "budget.sqlite3")
        await store.initialize()

        assert await store.notification_budget_available(
            "prime",
            cooldown_minutes=180,
            max_per_day=3,
        )
        await store.mark_notification_budget("prime")
        assert not await store.notification_budget_available(
            "prime",
            cooldown_minutes=180,
            max_per_day=3,
        )

    asyncio.run(check())


def test_strict_alert_cooldown_ignores_large_price_move(tmp_path) -> None:
    async def check() -> None:
        store = SignalStore(tmp_path / "strict.sqlite3")
        await store.initialize()

        assert await store.strict_alert_allowed("PRIME:TESTUSDT:LONG", 720)
        await store.mark_event_alerted("PRIME:TESTUSDT:LONG", 100.0)
        assert not await store.strict_alert_allowed("PRIME:TESTUSDT:LONG", 720)

    asyncio.run(check())
