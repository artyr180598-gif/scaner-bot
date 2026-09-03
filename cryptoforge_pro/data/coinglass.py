"""Optional Coinglass (CoinGlass) client.

Only enabled when ``COINGLASS_API_KEY`` is set. Endpoints follow the public v4
docs: https://open-api-v4.coinglass.com, header ``CG-API-KEY``.
Failure is non-fatal: the signal engine simply falls back to Binance/Bybit.
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from cryptoforge_pro.data.http import HttpSession
from cryptoforge_pro.models import Derivatives
from cryptoforge_pro.utils import coerce_float


class CoinglassProvider:
    def __init__(self, http: HttpSession, api_key: str, base_url: str) -> None:
        self.http = http
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._enabled = bool(api_key)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def funding_rate(self, symbol: str, interval: str = "1h", limit: int = 24) -> list[dict[str, Any]]:
        return await self._call(
            "/api/futures/fundingRate/ohlc-history",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )

    async def open_interest(
        self, symbol: str, interval: str = "1h", limit: int = 24
    ) -> list[dict[str, Any]]:
        return await self._call(
            "/api/futures/openInterest/ohlc-history",
            {"symbol": symbol, "interval": interval, "limit": limit, "unit": "usd"},
        )

    async def liquidation(
        self, exchange: str, symbol: str, interval: str = "1h", limit: int = 24
    ) -> list[dict[str, Any]]:
        return await self._call(
            "/api/futures/liquidation/history",
            {"exchange": exchange, "symbol": symbol, "interval": interval, "limit": limit},
        )

    async def derivatives_summary(self, symbol: str) -> Derivatives:
        out = Derivatives(source="coinglass")
        if not self._enabled:
            return out
        try:
            oi = await self.open_interest(symbol, interval="1h", limit=6)
            if oi:
                last = oi[-1] if isinstance(oi, list) else None
                if isinstance(last, dict):
                    val = last.get("close", last.get("value", last.get("oi")))
                    out.open_interest_value_usd = coerce_float(val) if val is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Coinglass OI failed for {}: {}", symbol, exc)
        try:
            fr = await self.funding_rate(symbol, interval="1h", limit=6)
            if fr:
                last = fr[-1] if isinstance(fr, list) else None
                if isinstance(last, dict):
                    val = last.get("close", last.get("fundingRate"))
                    out.funding_rate = coerce_float(val) if val is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Coinglass funding failed for {}: {}", symbol, exc)
        try:
            liq = await self.liquidation("Binance", symbol, interval="1h", limit=24)
            if liq:
                total = 0.0
                for row in liq:
                    if not isinstance(row, dict):
                        continue
                    val = row.get("totalLiquidationUsd", row.get("liquidationUsd", row.get("amountUsd")))
                    if val is not None:
                        total += coerce_float(val)
                if total > 0:
                    out.liquidation_24h_usd = total
        except Exception as exc:  # noqa: BLE001
            logger.debug("Coinglass liquidation failed for {}: {}", symbol, exc)
        return out

    async def _call(self, endpoint: str, params: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
        if not self._enabled:
            return None
        try:
            data = await self.http.get_json(
                f"{self.base_url}{endpoint}",
                params=params,
                headers={"CG-API-KEY": self.api_key},
                retries=1,
                timeout=10,
            )
            if isinstance(data, dict):
                payload = data.get("data")
                if isinstance(payload, list):
                    return payload
                return []
            return data if isinstance(data, list) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Coinglass {} failed: {}", endpoint, exc)
            return None
