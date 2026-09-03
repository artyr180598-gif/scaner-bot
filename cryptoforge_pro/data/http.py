"""Thin async HTTP client used by all market data providers."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

HEADERS = {
    "User-Agent": "CryptoForgePro/1.0 (+https://github.com/artyr180598-gif/scaner-bot)",
    "Accept": "application/json",
}


class HttpSession:
    def __init__(
        self,
        timeout: float = 10.0,
        max_connections: int = 20,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=timeout),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
            headers=HEADERS,
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(max_connections)
        self._started = False

    async def start(self) -> None:
        if not self._started:
            await self._client.__aenter__()
            self._started = True

    async def close(self) -> None:
        if self._started:
            await self._client.__aexit__(None, None, None)
            self._started = False

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 2,
        timeout: float | None = None,
    ) -> Any:
        """Perform a JSON request with a small retry loop.

        Raises ``httpx.HTTPError`` after retries are exhausted.
        """
        last_exc: Exception | None = None
        composite_headers = dict(HEADERS)
        if headers:
            composite_headers.update(headers)

        for attempt in range(retries + 1):
            try:
                async with self._sem:
                    response = await self._client.request(
                        method,
                        url,
                        params=params,
                        headers=composite_headers,
                        timeout=timeout,
                    )
                    if response.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"Server error {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    if not response.content:
                        return None
                    return response.json()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == retries:
                    break
                await asyncio.sleep(0.3 * (attempt + 1))
        assert isinstance(last_exc, Exception)
        logger.warning("HTTP request failed ({} {}): {}", method, url, last_exc)
        raise last_exc

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 2,
        timeout: float | None = None,
    ) -> Any:
        return await self.request_json(
            "GET", url, params=params, headers=headers, retries=retries, timeout=timeout
        )
