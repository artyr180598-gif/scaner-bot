from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime

from cryptopilot.config import Settings
from cryptopilot.exchange import INTERVAL_MS, ExchangeClient
from cryptopilot.models import Candle, Side
from cryptopilot.storage import SignalStore

log = logging.getLogger(__name__)


class PrimeShadowTracker:
    """Evaluate silent PRIME plans on later closed candles without notifying the user."""

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

    async def cycle(self) -> tuple[int, int, int]:
        observations = await self.store.open_prime_shadow()
        if not observations:
            self.status = "idle"
            return 0, 0, 0

        grouped: dict[str, list[dict]] = defaultdict(list)
        for item in observations:
            if item["exchange"] == self.exchange.name:
                grouped[item["symbol"]].append(item)

        fetched = await asyncio.gather(
            *(self.exchange.candles(symbol, "15", 400) for symbol in grouped),
            return_exceptions=True,
        )
        reviewed = entered = resolved = 0
        for (symbol, items), result in zip(grouped.items(), fetched, strict=True):
            if isinstance(result, BaseException):
                self.last_error = f"{symbol}: {type(result).__name__}"
                continue
            candles = result
            for item in items:
                reviewed += 1
                state = await self._resolve(item, candles)
                entered += state == "ENTERED"
                resolved += state in {"CLOSED", "EXPIRED"}

        self.status = "collecting"
        return reviewed, entered, resolved

    async def _resolve(self, item: dict, candles: list[Candle]) -> str:
        interval_ms = INTERVAL_MS["15"]
        created_at: datetime = item["created_at"]
        created_ms = int(created_at.timestamp() * 1000)
        first_bar = ((created_ms + interval_ms - 1) // interval_ms) * interval_ms
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        relevant = [
            candle
            for candle in candles
            if candle.open_time_ms >= first_bar
            and candle.open_time_ms + interval_ms <= now_ms
        ]
        if not relevant:
            return "UNCHANGED"

        entry_price = item["entry_price"]
        entry_at = item["entry_at"]
        just_entered = False
        for candle in relevant:
            bar_time = datetime.fromtimestamp(candle.open_time_ms / 1000, UTC)
            bar_closed_at = datetime.fromtimestamp(
                (candle.open_time_ms + interval_ms) / 1000,
                UTC,
            )
            if entry_price is None:
                if bar_time >= item["entry_expires_at"]:
                    await self.store.close_prime_shadow(
                        item["id"],
                        outcome="NO_ENTRY",
                        result_r=None,
                        exit_price=None,
                        closed_at=item["entry_expires_at"],
                        status="EXPIRED",
                    )
                    return "EXPIRED"
                if candle.high < item["entry_low"] or candle.low > item["entry_high"]:
                    continue
                entry_price = min(
                    max(candle.open, item["entry_low"]),
                    item["entry_high"],
                )
                entry_at = bar_time
                await self.store.mark_prime_shadow_entry(
                    item["id"],
                    entry_price,
                    entry_at,
                )
                just_entered = True

            assert entry_at is not None
            if bar_time < entry_at:
                continue
            stop_hit = (
                candle.low <= item["stop_loss"]
                if item["side"] is Side.LONG
                else candle.high >= item["stop_loss"]
            )
            target_hit = (
                candle.high >= item["take_profit"]
                if item["side"] is Side.LONG
                else candle.low <= item["take_profit"]
            )
            # Conservative OHLC rule: if SL and TP are both inside one bar, SL wins.
            if stop_hit:
                await self._close(
                    item,
                    entry_price,
                    item["stop_loss"],
                    bar_closed_at,
                    "SL",
                )
                return "CLOSED"
            if target_hit:
                await self._close(
                    item,
                    entry_price,
                    item["take_profit"],
                    bar_closed_at,
                    "TP2",
                )
                return "CLOSED"
            if bar_closed_at >= item["exit_expires_at"]:
                await self._close(
                    item,
                    entry_price,
                    candle.close,
                    bar_closed_at,
                    "TIME",
                )
                return "CLOSED"

        if entry_price is None and datetime.now(UTC) >= item["entry_expires_at"]:
            await self.store.close_prime_shadow(
                item["id"],
                outcome="NO_ENTRY",
                result_r=None,
                exit_price=None,
                closed_at=item["entry_expires_at"],
                status="EXPIRED",
            )
            return "EXPIRED"
        return "ENTERED" if just_entered else "UNCHANGED"

    async def _close(
        self,
        item: dict,
        entry: float,
        exit_price: float,
        closed_at: datetime,
        outcome: str,
    ) -> None:
        risk = (
            entry - item["stop_loss"]
            if item["side"] is Side.LONG
            else item["stop_loss"] - entry
        )
        if risk <= 0:
            result_r = -1.0
        else:
            gross = (
                (exit_price - entry) / risk
                if item["side"] is Side.LONG
                else (entry - exit_price) / risk
            )
            cost_fraction = self.settings.paper_one_way_cost_bps / 10_000
            costs_r = (entry + exit_price) * cost_fraction / risk
            result_r = gross - costs_r
        await self.store.close_prime_shadow(
            item["id"],
            outcome=outcome,
            result_r=result_r,
            exit_price=exit_price,
            closed_at=closed_at,
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                reviewed, entered, resolved = await self.cycle()
                if reviewed:
                    log.info(
                        "PRIME shadow: reviewed=%d entered=%d resolved=%d",
                        reviewed,
                        entered,
                        resolved,
                    )
                self.last_error = None
            except Exception as exc:
                self.status = "data error"
                self.last_error = str(exc)
                log.exception("PRIME shadow cycle failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=300)
