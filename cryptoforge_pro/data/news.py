"""CryptoPanic / news provider. Optional — only active with an API key."""

from __future__ import annotations

from typing import Any

from loguru import logger

from cryptoforge_pro.data.http import HttpSession


class NewsProvider:
    def __init__(self, http: HttpSession, api_key: str, language: str = "en") -> None:
        self.http = http
        self.api_key = api_key
        self.language = language
        self._enabled = bool(api_key)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def headlines(self, symbol: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        if not self._enabled:
            return []
        params: dict[str, Any] = {
            "auth_token": self.api_key,
            "public": "true",
            "collapsed": "true",
            "kind": "news",
            "language": self.language,
            "limit": min(limit, 50),
        }
        if symbol:
            params["currencies"] = symbol.replace("USDT", "").replace("USD", "")
        try:
            data = await self.http.get_json(
                "https://cryptopanic.com/api/v1/posts/",
                params=params,
                retries=1,
                timeout=10,
            )
            results = (data or {}).get("results") or []
            out: list[dict[str, Any]] = []
            for item in results[:limit]:
                out.append(
                    {
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "source": item.get("source", {}).get("title") if isinstance(item.get("source"), dict) else None,
                        "published": item.get("published_at"),
                        "created": item.get("created_at"),
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.debug("CryptoPanic news failed: {}", exc)
            return []
