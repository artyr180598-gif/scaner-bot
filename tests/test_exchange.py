from __future__ import annotations

import asyncio

import pytest

from cryptopilot.exchange import BinanceClient, BybitClient
from cryptopilot.models import Ticker


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



class FakeBybitHttp:
    async def get(self, path: str, params=None):
        params = params or {}
        category = params.get("category")
        if path.endswith("open-interest"):
            result = {
                "list": [
                    {"timestamp": "1", "openInterest": "1000000"},
                    {"timestamp": "2", "openInterest": "1020000"},
                ]
            }
        elif path.endswith("recent-trade") and category == "linear":
            result = {
                "list": [
                    {"price": "100", "size": "10", "side": "Buy"},
                    {"price": "100", "size": "5", "side": "Sell"},
                ]
            }
        elif path.endswith("orderbook") and category == "linear":
            result = {"b": [["100", "12"]], "a": [["100", "8"]]}
        elif path.endswith("account-ratio"):
            result = {"list": [{"buyRatio": "0.6", "sellRatio": "0.4"}]}
        elif path.endswith("recent-trade") and category == "spot":
            result = {
                "list": [
                    {
                        "price": "100",
                        "size": "1000",
                        "side": "Buy",
                        "isBlockTrade": True,
                    },
                    {
                        "price": "100",
                        "size": "200",
                        "side": "Sell",
                        "isBlockTrade": False,
                    },
                ]
            }
        elif path.endswith("orderbook") and category == "spot":
            result = {"b": [["100", "20"]], "a": [["100", "10"]]}
        elif path.endswith("tickers") and category == "spot":
            result = {"list": [{"lastPrice": "100"}]}
        else:
            raise AssertionError((path, params))
        return {"retCode": 0, "retMsg": "OK", "result": result}

    async def close(self) -> None:
        return None


def test_bybit_enrichment_detects_spot_led_accumulation() -> None:
    client = BybitClient("https://example.invalid", 5, 1)
    client.http = FakeBybitHttp()  # type: ignore[assignment]
    ticker = Ticker(
        symbol="TESTUSDT",
        last=100.05,
        bid=100.04,
        ask=100.06,
        turnover_24h=100_000_000,
        volume_24h=1_000_000,
        funding_rate=0.0001,
        open_interest=1_000_000,
    )

    enriched = asyncio.run(client.enrich_ticker(ticker))

    assert enriched.open_interest_change_pct == pytest.approx(2.0)
    assert enriched.spot_last == pytest.approx(100.0)
    assert enriched.spot_taker_buy_ratio == pytest.approx(5 / 6)
    assert enriched.spot_orderbook_imbalance == pytest.approx(1 / 3)
    assert enriched.spot_block_trade_buy_ratio == pytest.approx(1.0)
    assert enriched.spot_block_trade_notional == pytest.approx(100_000)
    assert enriched.spot_perp_basis_bps == pytest.approx(5.0)
