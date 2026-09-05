from __future__ import annotations

import asyncio

import pytest

from cryptopilot.exchange import BinanceClient


class FakeHttp:
    async def get(self, path: str, params=None):
        if path.endswith("exchangeInfo"):
            return {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                    }
                ]
            }
        if path.endswith("ticker/24hr"):
            return [
                {
                    "symbol": "BTCUSDT",
                    "lastPrice": "50000",
                    "quoteVolume": "1000000000",
                    "volume": "20000",
                }
            ]
        if path.endswith("premiumIndex"):
            return [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}]
        if path.endswith("bookTicker"):
            return [{"symbol": "BTCUSDT", "bidPrice": "49999", "askPrice": "50001"}]
        if path.endswith("openInterestHist"):
            return [
                {"timestamp": 1, "sumOpenInterestValue": "1000000"},
                {"timestamp": 2, "sumOpenInterestValue": "1050000"},
            ]
        if path.endswith("takerlongshortRatio"):
            return [{"buyVol": "60", "sellVol": "40"}]
        if path.endswith("/depth"):
            return {
                "bids": [["50000", "12"]],
                "asks": [["50000", "8"]],
            }
        if path.endswith("globalLongShortAccountRatio"):
            return [{"longShortRatio": "1.5"}]
        raise AssertionError(path)

    async def close(self) -> None:
        return None


def test_binance_combines_24h_funding_and_book_tickers() -> None:
    client = BinanceClient("https://example.invalid", 5, 1)
    client.http = FakeHttp()  # type: ignore[assignment]

    result = asyncio.run(client.tickers())

    assert len(result) == 1
    assert result[0].symbol == "BTCUSDT"
    assert result[0].bid == 49_999
    assert result[0].ask == 50_001
    assert result[0].funding_rate == 0.0001
    assert result[0].spread_bps < 1

    enriched = asyncio.run(client.enrich_ticker(result[0]))
    assert enriched.open_interest == 1_050_000
    assert enriched.open_interest_change_pct == pytest.approx(5)
    assert enriched.taker_buy_ratio == pytest.approx(0.6)
    assert enriched.orderbook_imbalance == pytest.approx(0.2)
    assert enriched.long_short_ratio == pytest.approx(1.5)
