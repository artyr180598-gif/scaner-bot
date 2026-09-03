"""Logging setup and small async helpers."""

from __future__ import annotations

import asyncio
import functools
import sys
from typing import Any, Callable

from loguru import logger


def configure_logging(level: str = "INFO") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    logger.info("CryptoForge Pro logging configured at {}", level.upper())


class TTLCache:
    """A tiny asyncio-safe TTL cache for market snapshots."""

    def __init__(self, ttl: float = 45.0, max_size: int = 512) -> None:
        self.ttl = ttl
        self.max_size = max_size
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at < asyncio.get_event_loop().time():
                self._store.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._store[key] = (asyncio.get_event_loop().time() + self.ttl, value)
            if len(self._store) > self.max_size:
                # drop the oldest requested keys
                for k in list(self._store)[: self.max_size // 4]:
                    self._store.pop(k, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


def retry_async(attempts: int = 3, delay: float = 0.5, backoff: float = 2.0):
    """Decorator that retries an async coroutine function on exceptions."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            wait = delay
            for i in range(attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - retry any transient error
                    last_exc = exc
                    if i == attempts - 1:
                        break
                    logger.debug("attempt {} failed for {}: {}", i + 1, func.__name__, exc)
                    await asyncio.sleep(wait)
                    wait *= backoff
            raise last_exc or RuntimeError("Retry exhausted")

        return wrapper

    return decorator


def coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
