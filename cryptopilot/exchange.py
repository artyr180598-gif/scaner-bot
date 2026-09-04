from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any

import aiohttp

from cryptopilot.models import Candle, Ticker

log = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    pass


INTERVAL_MS = {
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "30": 1_800_000,
    "60": 3_600_000,
    "120": 7_200_000,
    "240": 14_400_000,
    "D": 86_400_000,
}


class JsonClient:
    def __init__(self, base_url: str, timeout_seconds: float, concurrency: int) -> None:
        self.base_url = base_url
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": "CryptoPilot/3.0 market-intelligence"},
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self.start()
        assert self.session is not None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with (
                    self.semaphore,
                    self.session.get(f"{self.base_url}{path}", params=params) as response,
                ):
                    if response.status in {418, 429} or response.status >= 500:
                        raise MarketDataError(f"temporary HTTP {response.status}")
                    response.raise_for_status()
                    return await response.json()
            except (aiohttp.ClientError, TimeoutError, MarketDataError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise MarketDataError(f"GET {path} failed after retries: {last_error}")


class ExchangeClient(ABC):
    name: str

    @abstractmethod
    async def active_symbols(self) -> frozenset[str]: ...

    @abstractmethod
    async def tickers(self) -> list[Ticker]: ...

    @abstractmethod
    async def candles(self, symbol: str, interval: str, limit: int = 260) -> list[Candle]: ...

    async def enrich_ticker(self, ticker: Ticker) -> Ticker:
        """Attach optional derivatives context without making core market data fragile."""
        return ticker

    @abstractmethod
    async def ping(self) -> bool: ...

    @abstractmethod
    async def close(self) -> None: ...


class BybitClient(ExchangeClient):
    name = "BYBIT"

    def __init__(self, base_url: str, timeout_seconds: float, concurrency: int) -> None:
        self.http = JsonClient(base_url, timeout_seconds, concurrency)
        self._symbols: frozenset[str] | None = None

    @staticmethod
    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("retCode") != 0:
            raise MarketDataError(f"Bybit {payload.get('retCode')}: {payload.get('retMsg')}")
        return payload["result"]

    async def active_symbols(self) -> frozenset[str]:
        if self._symbols is not None:
            return self._symbols
        symbols: set[str] = set()
        cursor = ""
        while True:
            params: dict[str, Any] = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = self._result(await self.http.get("/v5/market/instruments-info", params))
            symbols.update(
                item["symbol"]
                for item in result.get("list", [])
                if item.get("status") == "Trading"
                and item.get("contractType") == "LinearPerpetual"
                and item.get("quoteCoin") == "USDT"
            )
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        self._symbols = frozenset(symbols)
        return self._symbols

    async def tickers(self) -> list[Ticker]:
        active = await self.active_symbols()
        result = self._result(await self.http.get("/v5/market/tickers", {"category": "linear"}))
        output: list[Ticker] = []
        for item in result.get("list", []):
            if item.get("symbol") not in active:
                continue
            try:
                output.append(
                    Ticker(
                        symbol=item["symbol"],
                        last=float(item["lastPrice"]),
                        bid=float(item["bid1Price"]),
                        ask=float(item["ask1Price"]),
                        turnover_24h=float(item.get("turnover24h") or 0),
                        volume_24h=float(item.get("volume24h") or 0),
                        funding_rate=float(item.get("fundingRate") or 0),
                        open_interest=float(item.get("openInterestValue") or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                log.debug("Skipping malformed Bybit ticker: %r", item)
        return output

    async def candles(self, symbol: str, interval: str, limit: int = 260) -> list[Candle]:
        result = self._result(
            await self.http.get(
                "/v5/market/kline",
                {
                    "category": "linear",
                    "symbol": symbol.upper(),
                    "interval": interval,
                    "limit": min(max(limit, 1), 1000),
                },
            )
        )
        now_ms = int(time.time() * 1000)
        duration = INTERVAL_MS[interval]
        rows = reversed(result.get("list", []))
        return [
            Candle(
                open_time_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                turnover=float(row[6]),
            )
            for row in rows
            if int(row[0]) + duration <= now_ms
        ]

    async def enrich_ticker(self, ticker: Ticker) -> Ticker:
        try:
            result = self._result(
                await self.http.get(
                    "/v5/market/open-interest",
                    {
                        "category": "linear",
                        "symbol": ticker.symbol,
                        "intervalTime": "1h",
                        "limit": 6,
                    },
                )
            )
            rows = sorted(result.get("list", []), key=lambda item: int(item["timestamp"]))
            values = [float(item["openInterest"]) for item in rows]
            change = _percentage_change(values[0], values[-1]) if len(values) >= 2 else None
            return replace(
                ticker,
                open_interest=values[-1] if values else ticker.open_interest,
                open_interest_change_pct=change,
            )
        except (KeyError, TypeError, ValueError, MarketDataError, aiohttp.ClientError) as exc:
            log.debug("Bybit open-interest context unavailable for %s: %s", ticker.symbol, exc)
            return ticker

    async def ping(self) -> bool:
        return self._result(await self.http.get("/v5/market/time")) is not None

    async def close(self) -> None:
        await self.http.close()


class BinanceClient(ExchangeClient):
    name = "BINANCE"
    _intervals = {
        "5": "5m",
        "15": "15m",
        "30": "30m",
        "60": "1h",
        "120": "2h",
        "240": "4h",
        "D": "1d",
    }

    def __init__(self, base_url: str, timeout_seconds: float, concurrency: int) -> None:
        self.http = JsonClient(base_url, timeout_seconds, concurrency)
        self._symbols: frozenset[str] | None = None

    async def active_symbols(self) -> frozenset[str]:
        if self._symbols is None:
            payload = await self.http.get("/fapi/v1/exchangeInfo")
            self._symbols = frozenset(
                item["symbol"]
                for item in payload.get("symbols", [])
                if item.get("status") == "TRADING"
                and item.get("contractType") == "PERPETUAL"
                and item.get("quoteAsset") == "USDT"
            )
        return self._symbols

    async def tickers(self) -> list[Ticker]:
        active = await self.active_symbols()
        ticker_rows, premium_rows, book_rows = await asyncio.gather(
            self.http.get("/fapi/v1/ticker/24hr"),
            self.http.get("/fapi/v1/premiumIndex"),
            self.http.get("/fapi/v1/ticker/bookTicker"),
        )
        funding = {x["symbol"]: float(x.get("lastFundingRate") or 0) for x in premium_rows}
        books = {x["symbol"]: x for x in book_rows}
        output: list[Ticker] = []
        for item in ticker_rows:
            if item.get("symbol") not in active:
                continue
            try:
                book = books[item["symbol"]]
                output.append(
                    Ticker(
                        symbol=item["symbol"],
                        last=float(item["lastPrice"]),
                        bid=float(book["bidPrice"]),
                        ask=float(book["askPrice"]),
                        turnover_24h=float(item.get("quoteVolume") or 0),
                        volume_24h=float(item.get("volume") or 0),
                        funding_rate=funding.get(item["symbol"], 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                log.debug("Skipping malformed Binance ticker: %r", item)
        return output

    async def candles(self, symbol: str, interval: str, limit: int = 260) -> list[Candle]:
        rows = await self.http.get(
            "/fapi/v1/klines",
            {
                "symbol": symbol.upper(),
                "interval": self._intervals[interval],
                "limit": min(max(limit, 1), 1500),
            },
        )
        now_ms = int(time.time() * 1000)
        return [
            Candle(
                open_time_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                turnover=float(row[7]),
            )
            for row in rows
            if int(row[6]) < now_ms
        ]

    async def enrich_ticker(self, ticker: Ticker) -> Ticker:
        try:
            rows = await self.http.get(
                "/futures/data/openInterestHist",
                {"symbol": ticker.symbol, "period": "1h", "limit": 6},
            )
            ordered = sorted(rows, key=lambda item: int(item["timestamp"]))
            values = [float(item.get("sumOpenInterestValue") or 0) for item in ordered]
            values = [value for value in values if value > 0]
            change = _percentage_change(values[0], values[-1]) if len(values) >= 2 else None
            return replace(
                ticker,
                open_interest=values[-1] if values else ticker.open_interest,
                open_interest_change_pct=change,
            )
        except (KeyError, TypeError, ValueError, MarketDataError, aiohttp.ClientError) as exc:
            log.debug("Binance open-interest context unavailable for %s: %s", ticker.symbol, exc)
            return ticker

    async def ping(self) -> bool:
        await self.http.get("/fapi/v1/ping")
        return True

    async def close(self) -> None:
        await self.http.close()


def build_exchange(
    name: str, bybit_url: str, binance_url: str, timeout_seconds: float, concurrency: int
) -> ExchangeClient:
    if name == "bybit":
        return BybitClient(bybit_url, timeout_seconds, concurrency)
    if name == "binance":
        return BinanceClient(binance_url, timeout_seconds, concurrency)
    raise ValueError(f"Unsupported exchange: {name}")


def _percentage_change(first: float, last: float) -> float | None:
    if first <= 0:
        return None
    return (last / first - 1) * 100
