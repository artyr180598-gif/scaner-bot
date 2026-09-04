from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from cryptopilot.config import Settings
from cryptopilot.exchange import ExchangeClient
from cryptopilot.models import Candle, Side, Signal, Ticker, TradePlan
from cryptopilot.paper import PaperTracker
from cryptopilot.storage import SignalStore


class PaperExchange(ExchangeClient):
    name = "BYBIT"

    def __init__(self, candles: list[Candle]) -> None:
        self.series = candles

    async def active_symbols(self) -> frozenset[str]:
        return frozenset({"TESTUSDT"})

    async def tickers(self) -> list[Ticker]:
        return []

    async def candles(self, symbol: str, interval: str, limit: int = 260) -> list[Candle]:
        return self.series

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def test_paper_tracker_enters_and_calibrates_closed_signal(tmp_path) -> None:
    async def scenario() -> None:
        store = SignalStore(tmp_path / "signals.sqlite3")
        await store.initialize()
        created = datetime(2026, 1, 1, tzinfo=UTC)
        plan = TradePlan(
            entry_low=100,
            entry_high=101,
            stop_loss=98,
            take_profit_1=103,
            take_profit_2=106,
            take_profit_3=109,
            risk_reward_2=2,
            invalidation="test",
            expires_at=created + timedelta(minutes=30),
            suggested_notional=100,
            suggested_quantity=1,
            risk_amount=2.5,
        )
        signal = Signal(
            symbol="TESTUSDT",
            exchange="BYBIT",
            side=Side.LONG,
            confidence=86,
            score=70,
            regime="BULL",
            price=100.5,
            created_at=created,
            plan=plan,
            required_confidence=84,
        )
        await store.mark_alerted(signal, max_holding_hours=72)
        candles = [
            Candle(1767225600000, 100.5, 102, 100, 101, 1000),
            Candle(1767226500000, 101, 106.5, 99, 106, 1000),
        ]
        settings = Settings(
            _env_file=None,
            telegram_bot_token="test",
            telegram_chat_id="1",
            data_dir=tmp_path,
        )
        result = await PaperTracker(PaperExchange(candles), store, settings).refresh()
        stats = await store.calibration()

        assert result.closed == 1
        assert await store.active_paper_count() == 0
        assert stats.sample_size == 1
        assert stats.wins == 1
        assert stats.expectancy_r > 1.5

    asyncio.run(scenario())


def test_paper_tracker_uses_conservative_stop_first(tmp_path) -> None:
    async def scenario() -> None:
        store = SignalStore(tmp_path / "signals.sqlite3")
        await store.initialize()
        created = datetime(2026, 1, 1, tzinfo=UTC)
        plan = TradePlan(
            entry_low=100,
            entry_high=101,
            stop_loss=98,
            take_profit_1=103,
            take_profit_2=106,
            take_profit_3=109,
            risk_reward_2=2,
            invalidation="test",
            expires_at=created + timedelta(minutes=30),
            suggested_notional=100,
            suggested_quantity=1,
            risk_amount=2.5,
        )
        signal = Signal(
            symbol="TESTUSDT",
            exchange="BYBIT",
            side=Side.LONG,
            confidence=86,
            score=70,
            regime="BULL",
            price=100.5,
            created_at=created,
            plan=plan,
        )
        await store.mark_alerted(signal)
        candle = Candle(1767225600000, 100.5, 107, 97, 100, 1000)
        settings = Settings(
            _env_file=None,
            telegram_bot_token="test",
            telegram_chat_id="1",
            data_dir=tmp_path,
        )
        await PaperTracker(PaperExchange([candle]), store, settings).refresh()
        stats = await store.calibration()

        assert stats.losses == 1
        assert stats.expectancy_r < 0

    asyncio.run(scenario())
