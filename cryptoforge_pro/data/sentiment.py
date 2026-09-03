"""Optional market sentiment source (Fear & Greed via alternative.me)."""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from cryptoforge_pro.data.http import HttpSession


class FearGreedProvider:
    def __init__(self, http: HttpSession) -> None:
        self.http = http

    async def index(self) -> Optional[dict[str, Any]]:
        try:
            raw = await self.http.get_json(
                "https://api.alternative.me/fng/",
                params={"limit": 1},
                retries=1,
                timeout=8,
            )
            data = (raw or {}).get("data") or []
            if not data:
                return None
            item = data[0]
            return {
                "value": int(float(item.get("value") or 0)),
                "classification": item.get("value_classification", ""),
                "timestamp": item.get("timestamp", ""),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("Fear & Greed unavailable: {}", exc)
            return None
