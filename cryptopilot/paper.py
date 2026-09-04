from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptopilot.config import Settings
from cryptopilot.exchange import INTERVAL_MS, ExchangeClient
from cryptopilot.models import Candle, PaperTrade, Side
from cryptopilot.storage import SignalStore

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PaperRefreshResult:
    reviewed: int = 0
    entered: int = 0
    closed: int = 0
    expired: int = 0
    errors: int = 0


class PaperTracker:
    """Resolve advisory signals against later closed candles for honest live statistics."""

    def __init__(
        self, exchange: ExchangeClient, store: SignalStore, settings: Settings
    ) -> None:
        self.exchange = exchange
        self.store = store
        self.settings = settings
        self._lock = asyncio.Lock()

    async def refresh(self) -> PaperRefreshResult:
        if not self.settings.paper_tracking_enabled:
            return PaperRefreshResult()
        async with self._lock:
            trades = await self.store.open_paper_trades()
            if not trades:
                return PaperRefreshResult()
            grouped: dict[str, list[PaperTrade]] = defaultdict(list)
            for trade in trades:
                if trade.exchange == self.exchange.name:
                    grouped[trade.symbol].append(trade)

            reviewed = entered = closed = expired = errors = 0
            results = await asyncio.gather(
                *(self._load_candles(symbol) for symbol in grouped),
                return_exceptions=True,
            )
            for (symbol, symbol_trades), result in zip(grouped.items(), results, strict=True):
                if isinstance(result, BaseException):
                    errors += len(symbol_trades)
                    log.warning("Paper tracking data failed for %s: %s", symbol, result)
                    continue
                for trade in symbol_trades:
                    reviewed += 1
                    outcome = await self._resolve(trade, result)
                    entered += outcome == "ENTERED"
                    closed += outcome == "CLOSED"
                    expired += outcome == "EXPIRED"
            return PaperRefreshResult(reviewed, entered, closed, expired, errors)

    async def _load_candles(self, symbol: str) -> list[Candle]:
        # 400 x 15m covers the default 72-hour holding window with a safety margin.
        return await self.exchange.candles(symbol, "15", 400)

    async def _resolve(self, trade: PaperTrade, candles: list[Candle]) -> str:
        interval_ms = INTERVAL_MS["15"]
        created_ms = int(trade.created_at.timestamp() * 1000)
        first_entry_bar = created_ms // interval_ms * interval_ms
        relevant = [item for item in candles if item.open_time_ms >= first_entry_bar]
        if not relevant:
            return "UNCHANGED"
        first_available = datetime.fromtimestamp(relevant[0].open_time_ms / 1000, UTC)
        if (
            trade.status == "OPEN"
            and trade.entry_at is not None
            and trade.entry_at < first_available
        ):
            await self.store.close_paper_trade(
                trade.id,
                outcome="DATA_GAP",
                result_r=None,
                exit_price=None,
                closed_at=first_available,
                status="EXPIRED",
            )
            return "EXPIRED"

        entry_price = trade.entry_price
        entry_at = trade.entry_at
        just_entered = False
        for candle in relevant:
            bar_time = datetime.fromtimestamp(candle.open_time_ms / 1000, UTC)
            bar_closed_at = datetime.fromtimestamp(
                (candle.open_time_ms + interval_ms) / 1000, UTC
            )
            if entry_price is None:
                if bar_time >= trade.entry_expires_at:
                    await self.store.close_paper_trade(
                        trade.id,
                        outcome="NO_ENTRY",
                        result_r=None,
                        exit_price=None,
                        closed_at=trade.entry_expires_at,
                        status="EXPIRED",
                    )
                    return "EXPIRED"
                if candle.high < trade.entry_low or candle.low > trade.entry_high:
                    continue
                entry_price = min(max(candle.open, trade.entry_low), trade.entry_high)
                entry_at = bar_time
                await self.store.mark_paper_entry(trade.id, entry_price, entry_at)
                just_entered = True

            assert entry_at is not None
            if bar_time < entry_at:
                continue
            stop_hit = (
                candle.low <= trade.stop_loss
                if trade.side is Side.LONG
                else candle.high >= trade.stop_loss
            )
            target_hit = (
                candle.high >= trade.take_profit
                if trade.side is Side.LONG
                else candle.low <= trade.take_profit
            )
            if stop_hit:
                await self._close(trade, entry_price, trade.stop_loss, bar_closed_at, "SL")
                return "CLOSED"
            if target_hit:
                await self._close(trade, entry_price, trade.take_profit, bar_closed_at, "TP2")
                return "CLOSED"
            if bar_closed_at >= trade.exit_expires_at:
                await self._close(trade, entry_price, candle.close, bar_closed_at, "TIME")
                return "CLOSED"

        if entry_price is None and datetime.now(UTC) >= trade.entry_expires_at:
            await self.store.close_paper_trade(
                trade.id,
                outcome="NO_ENTRY",
                result_r=None,
                exit_price=None,
                closed_at=trade.entry_expires_at,
                status="EXPIRED",
            )
            return "EXPIRED"
        return "ENTERED" if just_entered else "UNCHANGED"

    async def _close(
        self,
        trade: PaperTrade,
        entry: float,
        exit_price: float,
        closed_at: datetime,
        outcome: str,
    ) -> None:
        risk = (
            entry - trade.stop_loss
            if trade.side is Side.LONG
            else trade.stop_loss - entry
        )
        if risk <= 0:
            result_r = -1.0
        else:
            gross = (
                (exit_price - entry) / risk
                if trade.side is Side.LONG
                else (entry - exit_price) / risk
            )
            cost_fraction = self.settings.paper_one_way_cost_bps / 10_000
            costs_r = (entry + exit_price) * cost_fraction / risk
            result_r = max(-1.5, min(3.5, gross - costs_r))
        await self.store.close_paper_trade(
            trade.id,
            outcome=outcome,
            result_r=result_r,
            exit_price=exit_price,
            closed_at=closed_at,
        )
