"""Official REST clients for Binance and Bybit.

The bot only trusts public, official APIs. If an exchange is unreachable the
error is surfaced through ``MarketDataUnavailable`` — we never fabricate data.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable, Optional

import httpx
from loguru import logger

from cryptoforge_pro.data.http import HttpSession
from cryptoforge_pro.models import Candle, Derivatives, TickerInfo
from cryptoforge_pro.utils import coerce_float

BINANCE_SPOT_BASES = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"
BYBIT_TESTNET_BASE = "https://api-testnet.bybit.com"

INTERVAL_MAP_BINANCE = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h",
    "12h": "12h", "1d": "1d", "3d": "3d", "1w": "1w", "1M": "1M",
}

INTERVAL_MAP_BYBIT = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
    "1d": "D", "3d": "3D", "1w": "W", "1M": "M",
}


class MarketDataUnavailable(RuntimeError):
    """Raised when an upstream exchange cannot provide real market data."""


def _parse_binance_kline(row: list[Any]) -> Candle:
    return Candle(
        open=coerce_float(row[1]),
        high=coerce_float(row[2]),
        low=coerce_float(row[3]),
        close=coerce_float(row[4]),
        volume=coerce_float(row[5]),
        open_time_ms=int(row[0]),
        close_time_ms=int(row[6]),
    )


def _parse_bybit_kline(row: list[Any]) -> Candle:
    return Candle(
        open=coerce_float(row[1]),
        high=coerce_float(row[2]),
        low=coerce_float(row[3]),
        close=coerce_float(row[4]),
        volume=coerce_float(row[5]),
        open_time_ms=int(row[0]),
    )


class BinanceExchange:
    name = "binance"

    def __init__(self, http: HttpSession) -> None:
        self.http = http
        self._spot_base_url: str | None = None

    async def status(self) -> dict[str, Any]:
        try:
            base = await self._spot_base()
            return {"ok": True, "detail": f"Binance spot ping OK ({base})"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}

    async def _spot_base(self) -> str:
        if self._spot_base_url:
            return self._spot_base_url
        # Prefer api.binance.com, fall back to public data-api endpoint.
        for base in BINANCE_SPOT_BASES:
            try:
                await self.http.get_json(f"{base}/api/v3/ping", retries=0, timeout=5)
                self._spot_base_url = base
                return base
            except Exception:  # noqa: BLE001
                continue
        raise MarketDataUnavailable("Binance spot API is unreachable from this environment")

    async def get_klines(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        base = await self._spot_base()
        interval = INTERVAL_MAP_BINANCE.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported Binance interval: {timeframe}")
        raw = await self.http.get_json(
            f"{base}/api/v3/klines",
            params={"symbol": symbol.upper(), "interval": interval, "limit": min(limit, 1000)},
            retries=2,
        )
        if not isinstance(raw, list) or not raw:
            raise MarketDataUnavailable(f"Binance returned no candles for {symbol}")
        return [_parse_binance_kline(r) for r in raw]

    async def get_ticker(self, symbol: str) -> TickerInfo:
        base = await self._spot_base()
        data = await self.http.get_json(
            f"{base}/api/v3/ticker/24hr", params={"symbol": symbol.upper()}
        )
        if not isinstance(data, dict):
            raise MarketDataUnavailable(f"No ticker data for {symbol} on Binance")
        quote_price = coerce_float(data.get("quoteVolume"))
        return TickerInfo(
            symbol=data.get("symbol", symbol.upper()),
            base=data.get("symbol", symbol.upper()).replace("USDT", ""),
            quote="USDT",
            last_price=coerce_float(data.get("lastPrice")),
            high_24h=coerce_float(data.get("highPrice")),
            low_24h=coerce_float(data.get("lowPrice")),
            volume_24h_base=coerce_float(data.get("volume")),
            volume_24h_quote=quote_price,
            change_24h_pct=coerce_float(data.get("priceChangePercent")),
            change_1h_pct=0.0,
            source=self.name,
        )

    async def get_tickers_24h(self) -> list[TickerInfo]:
        base = await self._spot_base()
        raw = await self.http.get_json(f"{base}/api/v3/ticker/24hr", retries=2)
        if not isinstance(raw, list):
            raise MarketDataUnavailable("Binance ticker/24hr did not return a list")
        result: list[TickerInfo] = []
        for item in raw:
            sym = str(item.get("symbol", ""))
            if not sym.endswith("USDT"):
                continue
            try:
                last = coerce_float(item.get("lastPrice"))
            except (TypeError, ValueError):
                continue
            if last <= 0:
                continue
            result.append(
                TickerInfo(
                    symbol=sym,
                    base=sym[:-4],
                    quote="USDT",
                    last_price=last,
                    high_24h=coerce_float(item.get("highPrice")),
                    low_24h=coerce_float(item.get("lowPrice")),
                    volume_24h_base=coerce_float(item.get("volume")),
                    volume_24h_quote=coerce_float(item.get("quoteVolume")),
                    change_24h_pct=coerce_float(item.get("priceChangePercent")),
                    change_1h_pct=0.0,
                    source=self.name,
                )
            )
        return result

    async def get_derivative(self, symbol: str) -> Derivatives:
        """Best-effort Binance futures funding / OI. Never blocks a signal."""
        out = Derivatives(source=self.name)
        try:
            data = await self.http.get_json(
                f"{BINANCE_FUTURES_BASE}/fapi/v1/premiumIndex",
                params={"symbol": symbol.upper()},
                retries=1,
                timeout=8,
            )
            fr = data.get("lastFundingRate")
            out.funding_rate = coerce_float(fr) if fr is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Binance premiumIndex failed for {}: {}", symbol, exc)
        try:
            data = await self.http.get_json(
                f"{BINANCE_FUTURES_BASE}/fapi/v1/openInterest",
                params={"symbol": symbol.upper()},
                retries=1,
                timeout=8,
            )
            oi = data.get("openInterest")
            out.open_interest = coerce_float(oi) if oi is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Binance openInterest failed for {}: {}", symbol, exc)
        return out


class BybitExchange:
    name = "bybit"

    def __init__(self, http: HttpSession) -> None:
        self.http = http

    async def status(self) -> dict[str, Any]:
        try:
            await self.http.get_json(f"{BYBIT_BASE}/v5/market/time", retries=0, timeout=5)
            return {"ok": True, "detail": "Bybit v5 ping OK"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}

    async def get_klines(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        interval = INTERVAL_MAP_BYBIT.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported Bybit interval: {timeframe}")
        raw = await self.http.get_json(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "spot", "symbol": symbol.upper(), "interval": interval, "limit": min(limit, 1000)},
            retries=2,
        )
        rows = (raw.get("result") or {}).get("list") or []
        if not rows:
            raise MarketDataUnavailable(f"Bybit returned no candles for {symbol}")
        # Bybit returns newest first
        ordered = sorted(rows, key=lambda r: int(r[0]))
        return [_parse_bybit_kline(r) for r in ordered]

    async def get_ticker(self, symbol: str) -> TickerInfo:
        raw = await self.http.get_json(
            f"{BYBIT_BASE}/v5/market/tickers",
            params={"category": "spot", "symbol": symbol.upper()},
            retries=2,
        )
        rows = (raw.get("result") or {}).get("list") or []
        if not rows:
            raise MarketDataUnavailable(f"No ticker data for {symbol} on Bybit")
        item = rows[0]
        last = coerce_float(item.get("lastPrice"))
        return TickerInfo(
            symbol=item.get("symbol", symbol.upper()),
            base=item.get("baseCoin", symbol.replace("USDT", "")),
            quote=item.get("quoteCoin", "USDT"),
            last_price=last,
            high_24h=coerce_float(item.get("highPrice24h")),
            low_24h=coerce_float(item.get("lowPrice24h")),
            volume_24h_base=coerce_float(item.get("volume24h")),
            volume_24h_quote=coerce_float(item.get("turnover24h")),
            change_24h_pct=coerce_float(item.get("price24hPcnt")) * 100.0,
            change_1h_pct=0.0,
            source=self.name,
        )

    async def get_tickers_24h(self) -> list[TickerInfo]:
        raw = await self.http.get_json(
            f"{BYBIT_BASE}/v5/market/tickers",
            params={"category": "spot", "limit": 1000},
            retries=2,
        )
        rows = (raw.get("result") or {}).get("list") or []
        result: list[TickerInfo] = []
        for item in rows:
            sym = str(item.get("symbol", ""))
            if not sym.endswith("USDT"):
                continue
            last = coerce_float(item.get("lastPrice"))
            if last <= 0:
                continue
            result.append(
                TickerInfo(
                    symbol=sym,
                    base=item.get("baseCoin", sym[:-4]),
                    quote=item.get("quoteCoin", "USDT"),
                    last_price=last,
                    high_24h=coerce_float(item.get("highPrice24h")),
                    low_24h=coerce_float(item.get("lowPrice24h")),
                    volume_24h_base=coerce_float(item.get("volume24h")),
                    volume_24h_quote=coerce_float(item.get("turnover24h")),
                    change_24h_pct=coerce_float(item.get("price24hPcnt")) * 100.0,
                    change_1h_pct=0.0,
                    source=self.name,
                )
            )
        return result

    async def get_derivative(self, symbol: str) -> Derivatives:
        out = Derivatives(source=self.name)
        try:
            raw = await self.http.get_json(
                f"{BYBIT_BASE}/v5/market/tickers",
                params={"category": "linear", "symbol": symbol.upper()},
                retries=1,
                timeout=8,
            )
            rows = (raw.get("result") or {}).get("list") or []
            if rows:
                item = rows[0]
                fr = item.get("fundingRate")
                out.funding_rate = coerce_float(fr) if fr is not None else None
                oi = item.get("openInterest")
                out.open_interest = coerce_float(oi) if oi is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Bybit derivatives failed for {}: {}", symbol, exc)
        return out


class ExchangeRouter:
    """Combines multiple exchanges, normalised into one surface."""

    def __init__(self, exchanges: Iterable[BinanceExchange | BybitExchange]) -> None:
        self._exchanges = list(exchanges)
        self._binance = next((x for x in self._exchanges if x.name == "binance"), None)
        self._bybit = next((x for x in self._exchanges if x.name == "bybit"), None)
        if not self._binance and not self._bybit:
            raise ValueError("At least one exchange must be enabled")

    async def get_klines(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        errors: list[str] = []
        for exchange in self._exchanges:
            try:
                return await exchange.get_klines(symbol, timeframe, limit)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{exchange.name}: {exc}")
                continue
        raise MarketDataUnavailable("; ".join(errors) or "No exchange data")

    async def get_ticker(self, symbol: str) -> TickerInfo:
        errors: list[str] = []
        for exchange in self._exchanges:
            try:
                return await exchange.get_ticker(symbol)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{exchange.name}: {exc}")
        raise MarketDataUnavailable("; ".join(errors) or "No exchange data")

    async def get_tickers_24h(self) -> list[TickerInfo]:
        """Fetch from all enabled exchanges and merge preferring Binance."""
        tasks = [exchange.get_tickers_24h() for exchange in self._exchanges]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: dict[str, TickerInfo] = {}
        any_success = False
        for exchange, res in zip(self._exchanges, results):
            if isinstance(res, Exception):
                logger.warning("{} tickers failed: {}", exchange.name, res)
                continue
            any_success = True
            for ticker in res if isinstance(res, list) else []:
                # Prefer earlier exchange (binance first if enabled)
                if ticker.symbol not in merged or exchange.name in ("binance",):
                    merged[ticker.symbol] = ticker
        if not any_success:
            raise MarketDataUnavailable("All enabled exchanges failed to return real market tickers")
        return list(merged.values())

    async def get_derivative(self, symbol: str) -> Derivatives:
        for exchange in self._exchanges:
            if not hasattr(exchange, "get_derivative"):
                continue
            try:
                der = await exchange.get_derivative(symbol)  # type: ignore[attr-defined]
                if der.available:
                    return der
            except Exception as exc:  # noqa: BLE001
                logger.debug("derivatives {} failed for {}: {}", exchange.name, symbol, exc)
        return Derivatives(source="none")

    async def status(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for exchange in self._exchanges:
            status_fn = getattr(exchange, "status", None)
            if status_fn is None:
                out[exchange.name] = {"ok": False, "detail": "status not implemented"}
                continue
            try:
                out[exchange.name] = await status_fn()
            except Exception as exc:  # noqa: BLE001
                out[exchange.name] = {"ok": False, "detail": str(exc)}
        return out


def build_exchange_router(http: HttpSession, enabled: list[str]) -> ExchangeRouter:
    exchanges: list[BinanceExchange | BybitExchange] = []
    if "binance" in enabled:
        exchanges.append(BinanceExchange(http))
    if "bybit" in enabled:
        exchanges.append(BybitExchange(http))
    return ExchangeRouter(exchanges)
