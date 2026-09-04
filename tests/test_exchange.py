from __future__ import annotations

import asyncio

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
