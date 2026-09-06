"""Best-effort Bybit streaming radar.

The level-crossing path keeps the old conservative semantics. When a FlowTracker is supplied,
the same public socket also builds rolling taker-CVD and open-interest acceleration so the bot
can surface pressure *before* a structural trigger. These observations are not trade execution
instructions and cannot identify who is trading.
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

from cryptopilot.config import Settings
from cryptopilot.flow import FlowPressureEvent, FlowTracker
from cryptopilot.liquidity import LiquidityTracker
from cryptopilot.models import EarlySetup, Side

log = logging.getLogger(__name__)


def active_live_setups(
    report, now_seconds: float, threshold: int, max_age: int
) -> list[EarlySetup]:
    if report is None or not 0 <= now_seconds - report.finished_at.timestamp() <= max_age:
        return []
    return [
        s
        for s in report.setups
        if s.readiness >= threshold and s.actionable and s.expires_at.timestamp() > now_seconds
    ][:20]


def active_flow_candidates(
    early_report,
    smart_report,
    now_seconds: float,
    max_age: int,
    preselected: dict[str, tuple[Side, float]] | None = None,
    limit: int = 24,
) -> dict[str, tuple[Side, float]]:
    """Merge strongest structural candidates and cap websocket load deterministically."""
    result: dict[str, tuple[Side, float]] = {}
    if smart_report is not None:
        age = now_seconds - smart_report.finished_at.timestamp()
        if 0 <= age <= max_age:
            for setup in smart_report.setups[:16]:
                if (
                    setup.bias is not Side.NO_TRADE
                    and setup.stage != "ENTRY"
                    and setup.trigger_price > 0
                ):
                    result[setup.symbol] = (setup.bias, setup.trigger_price)
    if early_report is not None:
        age = now_seconds - early_report.finished_at.timestamp()
        if 0 <= age <= max_age:
            for setup in early_report.setups[:20]:
                if (
                    setup.actionable
                    and setup.expires_at.timestamp() > now_seconds
                    and setup.trigger_price > 0
                ):
                    result.setdefault(setup.symbol, (setup.bias, setup.trigger_price))
    for symbol, candidate in (preselected or {}).items():
        bias, trigger = candidate
        if bias is not Side.NO_TRADE and trigger > 0:
            result.setdefault(symbol, candidate)
        if len(result) >= limit:
            break
    return dict(list(result.items())[: max(1, limit)])


@dataclass(frozen=True)
class ActiveFlowValidation:
    observation_id: int
    symbol: str
    bias: Side
    trigger_price: float
    created_ms: int
    expires_ms: int


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
        *,
        flow_tracker: FlowTracker | None = None,
        liquidity_tracker: LiquidityTracker | None = None,
        flow_candidates: Callable[[], dict[str, tuple[Side, float]]] | None = None,
        send_flow: Callable[[FlowPressureEvent], Awaitable[None]] | None = None,
        on_flow_observed: Callable[[FlowPressureEvent], Awaitable[None]] | None = None,
        settings: Settings | None = None,
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

        self.flow_tracker = flow_tracker
        self.liquidity_tracker = liquidity_tracker
        self.flow_candidates = flow_candidates
        self.send_flow = send_flow
        self.on_flow_observed = on_flow_observed
        self.settings = settings
        self.flow_queue: asyncio.Queue[FlowPressureEvent] = asyncio.Queue(maxsize=20)
        self.flow_delivered = 0
        self.flow_observed = 0
        self.flow_dropped = 0
        self._flow_validation_active: dict[int, ActiveFlowValidation] = {}

    @property
    def flow_enabled(self) -> bool:
        return bool(
            self.flow_tracker is not None
            and self.flow_candidates is not None
            and self.send_flow is not None
            and self.settings is not None
            and self.settings.flow_radar_enabled
        )

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

    async def deliver_flow(self, event: FlowPressureEvent) -> None:
        assert self.settings is not None
        assert self.send_flow is not None
        allowed = await self.store.strict_alert_allowed(
            event.fingerprint,
            self.settings.flow_alert_cooldown_minutes,
        )
        if not allowed:
            self.flow_dropped += 1
            return

        if self.settings.flow_validation_enabled:
            observation_id = await self.store.record_flow_observation(
                symbol=event.symbol,
                bias=event.bias,
                score=event.score,
                event_type=event.event_type,
                event_price=event.price,
                trigger_price=event.trigger_price,
                created_at=datetime.fromtimestamp(event.created_ms / 1000, UTC),
                window_minutes=self.settings.flow_validation_window_minutes,
            )
            if observation_id > 0:
                self._flow_validation_active[observation_id] = ActiveFlowValidation(
                    observation_id=observation_id,
                    symbol=event.symbol,
                    bias=event.bias,
                    trigger_price=event.trigger_price,
                    created_ms=event.created_ms,
                    expires_ms=(
                        event.created_ms
                        + self.settings.flow_validation_window_minutes * 60_000
                    ),
                )

        await self.store.mark_event_alerted(event.fingerprint, event.price)
        self.flow_observed += 1

        if self.on_flow_observed is not None:
            try:
                await asyncio.wait_for(self.on_flow_observed(event), timeout=12)
            except Exception as exc:
                log.warning(
                    "Flow-triggered PRIME recheck failed for %s: %s",
                    event.symbol,
                    type(exc).__name__,
                )

        if self.settings.flow_auto_alerts_enabled:
            budget = await self.store.notification_budget_available(
                "flow",
                cooldown_minutes=self.settings.flow_global_cooldown_minutes,
                max_per_day=self.settings.flow_max_alerts_per_day,
            )
            if budget:
                await asyncio.wait_for(self.send_flow(event), timeout=5)
                await self.store.mark_notification_budget("flow")
                self.flow_delivered += 1
                log.info("Flow radar notified %s score=%d", event.symbol, event.score)
                return

        log.info(
            "Flow radar observed silently %s score=%d auto_alerts=%s",
            event.symbol,
            event.score,
            self.settings.flow_auto_alerts_enabled,
        )

    async def _resolve_live_flow_validation(
        self,
        symbol: str,
        price: float,
        event_ms: int,
    ) -> None:
        if not self._flow_validation_active:
            return
        for observation_id, item in list(self._flow_validation_active.items()):
            if item.symbol != symbol:
                continue
            if event_ms <= item.created_ms:
                continue
            if event_ms > item.expires_ms:
                self._flow_validation_active.pop(observation_id, None)
                continue
            hit = (
                price >= item.trigger_price
                if item.bias is Side.LONG
                else price <= item.trigger_price
            )
            if not hit:
                continue
            triggered_at = datetime.fromtimestamp(event_ms / 1000, UTC)
            await self.store.resolve_flow_observation(
                observation_id,
                status="TRIGGERED",
                resolved_at=datetime.now(UTC),
                triggered_at=triggered_at,
                lead_seconds=(event_ms - item.created_ms) / 1000,
            )
            self._flow_validation_active.pop(observation_id, None)

    async def _sender(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self.deliver(event)
            except Exception as exc:
                self.dropped += 1
                log.warning("Live radar delivery failed: %s", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def _flow_sender(self) -> None:
        while True:
            event = await self.flow_queue.get()
            try:
                await self.deliver_flow(event)
            except Exception as exc:
                self.flow_dropped += 1
                log.warning("Flow radar delivery failed: %s", type(exc).__name__)
            finally:
                self.flow_queue.task_done()

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
        flow_sender = asyncio.create_task(self._flow_sender()) if self.flow_enabled else None
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
            pending = [sender]
            if flow_sender is not None:
                flow_sender.cancel()
                pending.append(flow_sender)
            await asyncio.gather(*pending, return_exceptions=True)
            self.status = "stopped"

    async def _connection(self, session, stop: asyncio.Event) -> None:
        self.detector.clear_connection()
        async with session.ws_connect(self.url, heartbeat=20, timeout=10) as ws:
            self.status = "connected"
            log.info("Live radar connected; flow=%s", self.flow_enabled)
            subscribed_topics: set[str] = set()
            last_ping = time.monotonic()
            last_received = time.monotonic()

            while not stop.is_set():
                now = datetime.now(UTC)
                setups = {
                    s.symbol: s
                    for s in self.candidates()[:20]
                    if s.actionable and s.expires_at > now
                }
                flow_map = (
                    self.flow_candidates()
                    if self.flow_enabled and self.flow_candidates
                    else {}
                )
                stream_symbols = set(setups) | set(flow_map)
                desired_topics = {f"publicTrade.{s}" for s in stream_symbols}
                if self.flow_enabled:
                    desired_topics |= {f"tickers.{s}" for s in stream_symbols}
                if (
                    self.liquidity_tracker is not None
                    and self.settings is not None
                    and self.settings.liquidity_intelligence_enabled
                ):
                    desired_topics |= {
                        f"allLiquidation.{symbol}" for symbol in stream_symbols
                    }
                    orderbook_symbols = list(flow_map)[
                        : self.settings.liquidity_orderbook_watch_limit
                    ]
                    desired_topics |= {
                        f"orderbook.50.{symbol}" for symbol in orderbook_symbols
                    }

                if subscribed_topics - desired_topics:
                    await ws.send_json(
                        {
                            "op": "unsubscribe",
                            "args": sorted(subscribed_topics - desired_topics),
                        }
                    )
                if desired_topics - subscribed_topics:
                    await ws.send_json(
                        {
                            "op": "subscribe",
                            "args": sorted(desired_topics - subscribed_topics),
                        }
                    )
                subscribed_topics = desired_topics
                self.watching = len(stream_symbols)

                self.detector.previous = {
                    k: v for k, v in self.detector.previous.items() if k in setups
                }
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

                if topic.startswith("orderbook.50."):
                    self._handle_orderbook(payload)
                    continue
                if topic.startswith("allLiquidation."):
                    self._handle_liquidation(payload)
                    continue
                if topic.startswith("tickers.") and self.flow_enabled:
                    self._handle_ticker(topic, payload)
                    continue
                if not topic.startswith("publicTrade."):
                    continue

                symbol = topic.removeprefix("publicTrade.")
                trades = [
                    item
                    for item in payload.get("data", [])
                    if item.get("s") == symbol
                ]
                if not trades:
                    continue
                trades.sort(key=lambda item: int(item["T"]))
                setup = setups.get(symbol)

                for trade in trades:
                    event_ms = int(trade["T"])
                    price = float(trade["p"])
                    self.last_trade_ms = event_ms

                    if self.flow_enabled and self.flow_tracker is not None:
                        self.flow_tracker.add_trade(
                            symbol,
                            str(trade.get("S", "")),
                            price,
                            float(trade["v"]),
                            event_ms,
                            trade_id=str(trade.get("i") or "") or None,
                        )
                        if self.settings and self.settings.flow_validation_enabled:
                            await self._resolve_live_flow_validation(
                                symbol,
                                price,
                                event_ms,
                            )

                    if setup is not None:
                        crossing = self.detector.update(
                            setup,
                            price,
                            event_ms,
                            int(time.time() * 1000),
                        )
                        if crossing is not None:
                            try:
                                self.queue.put_nowait(crossing)
                            except asyncio.QueueFull:
                                self.dropped += 1
                                log.warning("Live radar queue full; dropping event")

                if (
                    self.flow_enabled
                    and self.flow_tracker is not None
                    and self.settings is not None
                ):
                    candidate = flow_map.get(symbol)
                    if candidate is not None:
                        bias, trigger_price = candidate
                        flow_event = self.flow_tracker.pressure_event(
                            symbol,
                            bias,
                            trigger_price,
                            min_notional_60s=self.settings.flow_min_notional_60s,
                            delta_threshold=self.settings.flow_delta_ratio_threshold,
                            burst_threshold=self.settings.flow_volume_burst_ratio,
                            min_oi_change_pct=self.settings.flow_min_oi_change_pct_2m,
                            min_score=self.settings.flow_min_alert_score,
                            max_spread_bps=self.settings.max_spread_bps,
                            max_directional_funding_pct=(
                                self.settings.flow_max_directional_funding_pct
                            ),
                            early_pressure_enabled=(
                                self.settings.flow_early_pressure_enabled
                            ),
                            early_pressure_min_score=(
                                self.settings.flow_early_pressure_min_score
                            ),
                            early_pressure_max_price_move_60s_pct=(
                                self.settings.flow_early_pressure_max_price_move_60s_pct
                            ),
                            early_pressure_min_burst_ratio=(
                                self.settings.flow_early_pressure_min_burst_ratio
                            ),
                            early_pressure_max_burst_ratio=(
                                self.settings.flow_early_pressure_max_burst_ratio
                            ),
                        )
                        if flow_event is not None:
                            try:
                                self.flow_queue.put_nowait(flow_event)
                            except asyncio.QueueFull:
                                self.flow_dropped += 1
                                log.warning("Flow queue full; dropping event")

    def _handle_orderbook(self, payload: dict) -> None:
        if self.liquidity_tracker is None:
            return
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return
        symbol = str(data.get("s") or "")
        if not symbol:
            return
        bids = data.get("b", [])
        asks = data.get("a", [])
        if not isinstance(bids, list) or not isinstance(asks, list):
            return
        self.liquidity_tracker.update_orderbook(
            symbol,
            str(payload.get("type") or "delta"),
            bids,
            asks,
            int(payload.get("ts") or time.time() * 1000),
        )

    def _handle_liquidation(self, payload: dict) -> None:
        if self.liquidity_tracker is None:
            return
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            try:
                symbol = str(item["s"])
                bybit_side = str(item["S"])
                price = float(item["p"])
                size = float(item["v"])
                event_ms = int(item["T"])
            except (KeyError, TypeError, ValueError):
                continue
            # Bybit: S=Buy means a LONG position was liquidated; Sell means SHORT.
            position_side = "LONG" if bybit_side == "Buy" else "SHORT"
            self.liquidity_tracker.add_liquidation(
                symbol,
                position_side,
                price,
                size,
                event_ms,
            )

    def _handle_ticker(self, topic: str, payload: dict) -> None:
        if self.flow_tracker is None:
            return
        symbol = topic.removeprefix("tickers.")
        raw = payload.get("data", {})
        data = raw[0] if isinstance(raw, list) and raw else raw
        if not isinstance(data, dict):
            return

        def number(name: str) -> float | None:
            value = data.get(name)
            if value in (None, ""):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        self.flow_tracker.add_ticker(
            symbol,
            int(payload.get("ts") or time.time() * 1000),
            last_price=number("lastPrice"),
            open_interest=number("openInterest"),
            open_interest_value=number("openInterestValue"),
            bid=number("bid1Price"),
            ask=number("ask1Price"),
            funding_rate=number("fundingRate"),
        )


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
