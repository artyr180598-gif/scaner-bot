import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptopilot.live_radar import Crossing, CrossingDetector, LiveRadar
from cryptopilot.models import EarlySetup, Side


def setup(side=Side.LONG):
    now = datetime.now(UTC)
    return EarlySetup(
        "TESTUSDT",
        "BYBIT",
        side,
        80,
        100,
        100,
        98,
        98,
        "WATCH",
        "BULL",
        now,
        now + timedelta(minutes=10),
    )


def test_crossing_requires_observed_transition_and_deduplicates():
    d = CrossingDetector()
    s = setup()
    t = int(time.time() * 1000)
    assert d.update(s, 100.1, t, t) is None  # already crossed at connect
    assert d.update(s, 99.9, t + 1, t + 1) is None
    e = d.update(s, 100.1, t + 2, t + 2)
    assert e and e.direction == "LONG"
    d.update(s, 99.9, t + 3, t + 3)
    assert d.update(s, 100.1, t + 4, t + 4) is None


def test_short_and_reconnect_and_gap():
    d = CrossingDetector()
    s = setup(Side.SHORT)
    t = int(time.time() * 1000)
    d.update(s, 100.1, t, t)
    d.clear_connection()
    assert d.update(s, 99.9, t + 1, t + 1) is None
    d.update(s, 100.1, t + 2, t + 2)
    assert d.update(s, 99.9, t + 6000, t + 6000) is None
    d.update(s, 100.1, t + 6001, t + 6001)
    assert d.update(s, 99.9, t + 6002, t + 6002).direction == "SHORT"


def test_stale_expired_wrong_order_and_overshoot():
    d = CrossingDetector()
    s = setup()
    t = int(time.time() * 1000)
    d.update(s, 99.9, t, t)
    assert d.update(s, 100.1, t - 1, t) is None
    assert d.update(s, 100.1, t + 1, t + 6001) is None
    d.update(s, 99.9, t + 6002, t + 6002)
    assert d.update(s, 101, t + 6003, t + 6003) is None
    expired = replace(s, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert d.update(expired, 100.1, t + 6004, t + 6004) is None


def test_delivery_ttl_and_persistence():
    async def check():
        send = AsyncMock()
        store = SimpleNamespace(set_runtime=AsyncMock())
        radar = LiveRadar(lambda: [], send, store)
        t = int(time.time() * 1000)
        e = Crossing("TESTUSDT", "LONG", 100, 100.1, t - 6000, t, t + 60000, "key")
        await radar.deliver(e)
        send.assert_not_awaited()
        fresh = replace(e, event_ms=t)
        await radar.deliver(fresh)
        await radar.deliver(fresh)
        assert send.await_count == 1 and store.set_runtime.await_count == 1

    asyncio.run(check())


def test_websocket_subscription_batches_and_nonblocking_queue():
    import json

    import aiohttp

    async def check():
        stop = asyncio.Event()
        t = int(time.time() * 1000)
        frames = [{"op": "subscribe", "success": True}]
        for i, p in enumerate(["99.9", "100.1"]):
            frames.append(
                {
                    "topic": "publicTrade.TESTUSDT",
                    "data": [{"s": "TESTUSDT", "p": p, "T": t - 2 + i}],
                }
            )

        class Socket:
            sent = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def send_json(self, value):
                self.sent.append(value)

            async def receive(self, timeout):
                item = frames.pop(0)
                if not frames:
                    stop.set()
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(item))

        socket = Socket()
        session = SimpleNamespace(ws_connect=lambda *args, **kwargs: socket)
        radar = LiveRadar(lambda: [setup()], AsyncMock(), None)
        await radar._connection(session, stop)
        assert socket.sent[0] == {"op": "subscribe", "args": ["publicTrade.TESTUSDT"]}
        assert radar.queue.qsize() == 1
        radar.send.assert_not_awaited()

    asyncio.run(check())


def test_watchlist_refresh_does_not_require_manual_button():
    from cryptopilot.live_radar import refresh_watchlist

    async def check():
        stop = asyncio.Event()

        async def scan():
            stop.set()

        scanner = SimpleNamespace(
            last_early_report=None,
            scan_lock=asyncio.Lock(),
            scan_early_moves=AsyncMock(side_effect=scan),
        )
        await refresh_watchlist(scanner, stop, 300)
        scanner.scan_early_moves.assert_awaited_once()

    asyncio.run(check())


def test_watchlist_must_be_recent_and_above_auto_threshold():
    from cryptopilot.live_radar import active_live_setups

    now = datetime.now(UTC)
    report = SimpleNamespace(finished_at=now, setups=[setup(), replace(setup(), readiness=79)])
    assert len(active_live_setups(report, now.timestamp(), 80, 600)) == 1
    assert active_live_setups(report, now.timestamp() + 601, 80, 600) == []
