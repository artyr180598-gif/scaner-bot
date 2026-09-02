"""
app/data/cache.py — простой async-safe TTL-кэш.

Нужен, чтобы 300 монет не превращались в 300×4 запроса к бирже: свечи одного
символа переиспользуются в пределах TTL, тикеры кэшируются целиком.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Hashable, Optional, Tuple


class TTLCache:
    """Кэш «ключ → (значение, время_истечения)» с защитой от stampede."""

    def __init__(self, ttl_seconds: float = 60.0, maxsize: int = 4096) -> None:
        self.ttl = float(ttl_seconds)
        self.maxsize = int(maxsize)
        self._data: Dict[Hashable, Tuple[Any, float]] = {}
        self._locks: Dict[Hashable, asyncio.Lock] = {}
        self._master = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> Optional[Any]:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        value, expires = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: Hashable, value: Any, ttl: Optional[float] = None) -> None:
        if len(self._data) >= self.maxsize:
            self._evict()
        self._data[key] = (value, time.monotonic() + (ttl if ttl is not None else self.ttl))

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._data.items() if exp < now]
        for k in expired:
            self._data.pop(k, None)
        if len(self._data) >= self.maxsize:
            # Всё ещё переполнен: выкидываем самые старые записи.
            for k, _ in sorted(self._data.items(), key=lambda kv: kv[1][1])[: self.maxsize // 4]:
                self._data.pop(k, None)

    async def get_or_set(self, key: Hashable,
                         producer: Callable[[], Awaitable[Any]],
                         ttl: Optional[float] = None) -> Any:
        """
        Возвращает значение из кэша или вычисляет его.

        Локи по ключу гарантируют, что при одновременном запросе одной и той же
        пары данные с биржи будут запрошены один раз, а не N раз.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        async with self._master:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.get(key)
            if cached is not None:
                return cached
            value = await producer()
            if value is not None:
                self.set(key, value, ttl)
            return value

    def clear(self) -> None:
        self._data.clear()
        self.hits = self.misses = 0

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "size": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }
