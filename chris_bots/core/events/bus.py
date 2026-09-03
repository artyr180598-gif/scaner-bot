"""
Простая шина событий на asyncio.

Подписчик регистрируется фильтром по типу события.
Шина буферизует события в очереди и доставляет их в `dispatch()`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Set

from .events import Event

log = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Handler]] = {}
        self._wildcard: List[Handler] = []
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        self._wildcard.append(handler)

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="event-bus")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _loop(self) -> None:
        log.debug("event bus started")
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await self._dispatch(event)
        log.debug("event bus stopped")

    async def _dispatch(self, event: Event) -> None:
        handlers: List[Handler] = list(self._subscribers.get(event.type, []))
        handlers += self._wildcard
        if not handlers:
            return
        results = await asyncio.gather(
            *(self._safe_call(h, event) for h in handlers), return_exceptions=False
        )
        for ok, err in results:
            if not ok and err:
                log.warning("handler error for %s: %s", event.type, err)

    @staticmethod
    async def _safe_call(handler: Handler, event: Event):
        try:
            await handler(event)
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, repr(exc)
