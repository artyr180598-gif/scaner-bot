from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime

from cryptopilot.config import Settings
from cryptopilot.exchange import ExchangeClient
from cryptopilot.models import Candle, Side
from cryptopilot.storage import SignalStore

log = logging.getLogger(__name__)


class FlowForwardValidator:
    """Measure whether an early flow alert actually precedes its structural trigger.

    This validates *lead detection*, not profitability. The signal minute itself is skipped
    because a REST candle cannot tell whether a high/low happened before or after the alert.
    """

    def __init__(
        self,
        exchange: ExchangeClient,
        store: SignalStore,
        settings: Settings,
    ) -> None:
        self.exchange = exchange
        self.store = store
        self.settings = settings
        self.status = "starting"
        self.last_error: str | None = None

    async def cycle(self) -> tuple[int, int]:
        observations = await self.store.pending_flow_observations(limit=100)
        if not observations:
            self.status = "idle"
            return 0, 0

        grouped: dict[str, list[dict]] = defaultdict(list)
        for item in observations:
            grouped[item["symbol"]].append(item)

        fetched = await asyncio.gather(
            *(self._candles(symbol) for symbol in grouped),
            return_exceptions=True,
        )
        now = datetime.now(UTC)
        resolved = triggered = 0
        for symbol, result in zip(grouped, fetched, strict=True):
            if isinstance(result, BaseException):
                self.last_error = f"{symbol}: {type(result).__name__}"
                continue
            candles = result
            for item in grouped[symbol]:
                trigger_time = _first_trigger_time(item, candles)
                if trigger_time is not None:
                    lead_seconds = max(
                        0.0,
                        (trigger_time - item["created_at"]).total_seconds(),
                    )
                    await self.store.resolve_flow_observation(
                        item["id"],
                        status="TRIGGERED",
                        resolved_at=now,
                        triggered_at=trigger_time,
                        lead_seconds=lead_seconds,
                    )
                    resolved += 1
                    triggered += 1
                elif now >= item["expires_at"]:
                    await self.store.resolve_flow_observation(
                        item["id"],
                        status="EXPIRED",
                        resolved_at=now,
                    )
                    resolved += 1

        self.status = "collecting"
        if resolved:
            log.info(
                "Flow forward validation: resolved=%d triggered=%d",
                resolved,
                triggered,
            )
        return resolved, triggered

    async def _candles(self, symbol: str) -> list[Candle]:
        # 120 closed 1m bars safely cover the default 45m validation horizon.
        return await self.exchange.candles(symbol, "1", 120)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.cycle()
                self.last_error = None
            except Exception as exc:
                self.status = "data error"
                self.last_error = str(exc)
                log.exception("Flow forward validation cycle failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=60)


def _first_trigger_time(item: dict, candles: list[Candle]) -> datetime | None:
    created_at: datetime = item["created_at"].astimezone(UTC)
    expires_at: datetime = item["expires_at"].astimezone(UTC)
    created_ms = int(created_at.timestamp() * 1000)
    expires_ms = int(expires_at.timestamp() * 1000)

    # Ignore the candle that was already open when the alert was created. This prevents
    # attributing a pre-alert wick to the alert.
    first_safe_open_ms = ((created_ms // 60_000) + 1) * 60_000
    trigger = float(item["trigger_price"])
    long = item["bias"] is Side.LONG

    for candle in candles:
        if candle.open_time_ms < first_safe_open_ms:
            continue
        close_ms = candle.open_time_ms + 60_000
        if close_ms > expires_ms:
            break
        hit = candle.high >= trigger if long else candle.low <= trigger
        if hit:
            # Closed-bar timestamp is intentionally conservative: exact intraminute hit time
            # is not available from the kline endpoint.
            return datetime.fromtimestamp(close_ms / 1000, UTC)
    return None
