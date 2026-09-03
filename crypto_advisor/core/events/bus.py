"""Лёгкая асинхронная шина событий (типовая идея из Nautilus Trader)."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable, Coroutine, Dict, List

log = logging.getLogger(__name__)

Handler = Callable[[Any], Coroutine[Any, Any, Any]]


class EventBus:
    """EventBus на asyncio. Подписчики получают события по имени типа."""

    def __init__(self) -> None:
        self._subs: Dict[str, List[Handler]] = {}

    async def start(self) -> None:
        self._subs.clear()

    async def stop(self) -> None:
        self._subs.clear()

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subs.setdefault(event_type, []).append(handler)

    async def publish(self, event: Any) -> None:
        """Публикует событие всем подписчикам его типа (best-effort)."""
        name = type(event).__name__
        for handler in self._subs.get(name, []):
            try:
                await handler(event)
            except Exception as exc:  # noqa: BLE001
                log.warning("event handler %s failed for %s: %s", name, handler, exc)
