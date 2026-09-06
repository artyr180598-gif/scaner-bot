from __future__ import annotations

import asyncio
from dataclasses import replace

from cryptopilot.cross_exchange import verify_cross_exchange
from cryptopilot.indicators import compute_features
from cryptopilot.models import Side, Ticker


class FakeExchange:
    name = "BINANCE"

    def __init__(self, candle_factory):
        self.candle_factory = candle_factory
        feature = compute_features(candle_factory(direction=1))
        self.ticker = Ticker(
            symbol="TESTUSDT",
            last=feature.close,
            bid=feature.close * 0.9999,
            ask=feature.close * 1.0001,
            turnover_24h=100_000_000,
            volume_24h=1_000_000,
            open_interest_change_pct=1.5,
            taker_buy_ratio=0.62,
        )

    async def enrich_ticker(self, ticker):
        return replace(
            ticker,
            open_interest_change_pct=1.5,
            taker_buy_ratio=0.62,
        )

    async def candles(self, symbol, interval, limit=260):
        return self.candle_factory(interval=interval, direction=1)


def test_cross_exchange_confirmation_counts_independent_evidence(candle_factory) -> None:
    exchange = FakeExchange(candle_factory)
    result = asyncio.run(
        verify_cross_exchange(
            exchange,
            exchange.ticker,
            Side.LONG,
            exchange.ticker.last,
        )
    )

    assert result.exchange == "BINANCE"
    assert result.confirmations >= 3
    assert result.conflicts == 0
    assert result.price_divergence_bps == 0
    assert result.reasons
