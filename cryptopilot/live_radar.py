"""Best-effort Bybit level-crossing notifications, NOT trade recommendations.

Candidates come from the existing early radar. A stream removes the scan timer
from crossing detection, not the selection latency or uncertainty of a setup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

from cryptopilot.models import EarlySetup, Side

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Crossing:
    symbol: str
    direction: str
    level: float
    price: float
    event_ms: int
    received_ms: int
    expires_ms: int
    key: str


class CrossingDetector:
    def __init__(self) -> None:
        self.previous: dict[str, tuple[str, float, int]] = {}
        self.emitted: set[str] = set()

    def clear_connection(self) -> None:
        # Never infer a crossing across an unobserved connection gap.
        self.previous.clear()

    def update(
        self, setup: EarlySetup, price: float, event_ms: int, now_ms: int
    ) -> Crossing | None:
        expiry = int(setup.expires_at.timestamp() * 1000)
        level = setup.trigger_price
        if (
            not setup.actionable
            or expiry <= now_ms
            or not math.isfinite(price)
            or not math.isfinite(level)
            or min(price, level) <= 0
            or not 0 <= now_ms - event_ms <= 5000
        ):
            self.previous.pop(setup.symbol, None)
            return None
        key = f"{setup.symbol}:{setup.bias.value}:{level:.12g}"
        previous = self.previous.get(setup.symbol)
        if previous and event_ms <= previous[2]:
            return None
        self.previous[setup.symbol] = (key, price, event_ms)
        if previous is None or previous[0] != key or event_ms - previous[2] > 5000:
            return None
        sign = 1 if setup.bias is Side.LONG else -1
        crossed = sign * (previous[1] - level) <= 0 < sign * (price - level)
        if not crossed or abs(price / level - 1) > 0.003 or key in self.emitted:
            return None
        self.emitted.add(key)
        return Crossing(setup.symbol, setup.bias.value, level, price, event_ms, now_ms, expiry, key)


class LiveRadar:
    def __init__(
        self,
        candidates: Callable[[], list[EarlySetup]],
        send: Callable[[Crossing], Awaitable[None]],
        store,
        url: str = "wss://stream.bybit.com/v5/public/linear",
    ) -> None:
        self.candidates = candidates
        self.send = send
        self.store = store
        self.url = url
        self.detector = CrossingDetector()
        self.queue: asyncio.Queue[Crossing] = asyncio.Queue(maxsize=20)
        self.sent: deque[str] = deque(maxlen=256)
        self.status = "starting"
        self.delivered = 0
        self.dropped = 0
        self.watching = 0
        self.last_trade_ms: int | None = None

    async def deliver(self, event: Crossing) -> None:
        now_ms = int(time.time() * 1000)
        if event.key in self.sent or now_ms - event.event_ms > 5000 or now_ms >= event.expires_ms:
            self.dropped += 1
            return
        await asyncio.wait_for(self.send(event), timeout=5)
        self.sent.append(event.key)
        self.delivered += 1
        await self.store.set_runtime("live_radar_recent", json.dumps(list(self.sent)))
        log.info(
            "Live radar delivered %s: event_to_api_ack_ms=%d",
            event.symbol,
            int(time.time() * 1000) - event.event_ms,
        )

    async def _sender(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self.deliver(event)
            except Exception as exc:
                self.dropped += 1
                # No stale retry or retry storm; delivery is explicitly best effort.
                log.warning("Live radar delivery failed: %s", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def run(self, stop: asyncio.Event) -> None:
        recent = await self.store.get_runtime("live_radar_recent")
        if recent:
            try:
                saved = json.loads(recent[0])
                if isinstance(saved, list):
                    self.sent.extend(x for x in saved[-256:] if isinstance(x, str))
            except (ValueError, TypeError):
                log.warning("Invalid live radar dedup state; starting empty")
        sender = asyncio.create_task(self._sender())
        backoff = 1
        try:
            async with aiohttp.ClientSession() as session:
                while not stop.is_set():
                    try:
                        await self._connection(session, stop)
                        backoff = 1
                    except aiohttp.WSServerHandshakeError as exc:
                        if exc.status in (401, 403, 429):
                            self.status = "access_blocked"
                            log.error("Live radar stopped: HTTP %d", exc.status)
                            await stop.wait()
                            return
                        log.warning("Live radar handshake failed: HTTP %d", exc.status)
                    except PermissionError:
                        self.status = "subscription_rejected"
                        log.error("Live radar stopped: subscription rejected")
                        await stop.wait()
                        return
                    except (
                        aiohttp.ClientError,
                        TimeoutError,
                        ValueError,
                        KeyError,
                        TypeError,
                    ) as exc:
                        self.status = "reconnecting"
                        log.warning("Live radar reconnect: %s", type(exc).__name__)
                    if not stop.is_set():
                        with suppress(TimeoutError):
                            await asyncio.wait_for(stop.wait(), timeout=backoff)
                        backoff = min(30, backoff * 2)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            self.status = "stopped"

    async def _connection(self, session, stop: asyncio.Event) -> None:
        self.detector.clear_connection()
        async with session.ws_connect(self.url, heartbeat=20, timeout=10) as ws:
            self.status = "connected"
            log.info("Live radar connected: level events only, not entry signals")
            subscribed: set[str] = set()
            last_ping = time.monotonic()
            last_received = time.monotonic()
            while not stop.is_set():
                now = datetime.now(UTC)
                setups = {
                    s.symbol: s
                    for s in self.candidates()[:20]
                    if s.actionable and s.expires_at > now
                }
                symbols = set(setups)
                if subscribed - symbols:
                    await ws.send_json(
                        {
                            "op": "unsubscribe",
                            "args": [f"publicTrade.{s}" for s in sorted(subscribed - symbols)],
                        }
                    )
                if symbols - subscribed:
                    await ws.send_json(
                        {
                            "op": "subscribe",
                            "args": [f"publicTrade.{s}" for s in sorted(symbols - subscribed)],
                        }
                    )
                subscribed = symbols
                self.watching = len(symbols)
                self.detector.previous = {
                    k: v for k, v in self.detector.previous.items() if k in symbols
                }
                # Bound memory; retain active keys plus the durable recent-delivery window.
                active_keys = {
                    f"{s.symbol}:{s.bias.value}:{s.trigger_price:.12g}" for s in setups.values()
                }
                self.detector.emitted.intersection_update(active_keys | set(self.sent))
                if time.monotonic() - last_ping >= 20:
                    await ws.send_json({"op": "ping"})
                    last_ping = time.monotonic()
                if time.monotonic() - last_received > 45:
                    raise TimeoutError("No stream messages or heartbeat replies")
                try:
                    msg = await ws.receive(timeout=2)
                except TimeoutError:
                    continue
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise aiohttp.ClientConnectionError("Stream closed")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                last_received = time.monotonic()
                payload = json.loads(msg.data)
                if payload.get("success") is False:
                    raise PermissionError("Subscription operation rejected")
                topic = payload.get("topic", "")
                if not topic.startswith("publicTrade."):
                    continue
                symbol = topic.removeprefix("publicTrade.")
                setup = setups.get(symbol)
                if setup is None:
                    continue
                # Use latest execution in a batch, not an earlier transient crossing.
                trades = payload.get("data", [])
                if not trades:
                    continue
                trade = trades[-1]
                if trade.get("s") != symbol:
                    continue
                self.last_trade_ms = int(trade["T"])
                event = self.detector.update(
                    setup, float(trade["p"]), int(trade["T"]), int(time.time() * 1000)
                )
                if event is not None:
                    try:
                        self.queue.put_nowait(event)
                    except asyncio.QueueFull:
                        self.dropped += 1
                        log.warning("Live radar queue full; dropping event")


async def refresh_watchlist(scanner, stop: asyncio.Event, interval_seconds: int = 300) -> None:
    """Refresh discovery separately from streamed crossings, without duplicate fresh scans."""
    while not stop.is_set():
        report = scanner.last_early_report
        age = (datetime.now(UTC) - report.finished_at).total_seconds() if report else float("inf")
        if age >= interval_seconds and not scanner.scan_lock.locked():
            try:
                await scanner.scan_early_moves()
                log.info("Live radar watchlist refreshed")
            except Exception as exc:
                log.warning("Live watchlist refresh failed: %s", type(exc).__name__)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=30)
