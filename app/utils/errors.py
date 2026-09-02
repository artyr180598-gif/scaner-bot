"""
app/utils/errors.py — типы ошибок и обёртки устойчивости.

Принцип: одна недоступная биржа не должна ронять прогон сканера. Все сетевые
операции заворачиваются в ``retry_async``, а деградация помечается в
``data_confidence`` / ``warnings``, а не исключением.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from typing import Any, Awaitable, Callable, Iterable, Optional, Tuple, Type

log = logging.getLogger(__name__)


class AdvisorError(Exception):
    """Базовая ошибка приложения."""


class ConfigError(AdvisorError):
    """Некорректная конфигурация (.env)."""


class DataError(AdvisorError):
    """Не удалось получить данные рынка."""


class ExchangeUnavailable(DataError):
    """Биржа недоступна (все биржи из списка отказали)."""


class InsufficientData(DataError):
    """Слишком мало истории для честного анализа."""


class AnalysisError(AdvisorError):
    """Ошибка внутри модуля анализа (не фатальна — модуль пропускается)."""


DEFAULT_RETRY_ON: Tuple[Type[BaseException], ...] = (
    asyncio.TimeoutError, ConnectionError, OSError, DataError,
)


def retry_async(
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    jitter: float = 0.25,
    exceptions: Iterable[Type[BaseException]] = DEFAULT_RETRY_ON,
    logger: Optional[logging.Logger] = None,
    default: Any = None,
    reraise: bool = False,
) -> Callable:
    """
    Декоратор повторов для async-функций с экспоненциальной задержкой.

    ``reraise=False`` + ``default`` — «тихая деградация»: после последней
    попытки возвращаем значение по умолчанию (используется для опциональных
    данных вроде funding/OI).
    """
    exc_tuple = tuple(exceptions)
    _log = logger or log

    def decorator(func: Callable[..., Awaitable[Any]]):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last: Optional[BaseException] = None
            wait = delay
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exc_tuple as exc:  # noqa: PERF203
                    last = exc
                    if attempt == attempts:
                        break
                    sleep_for = wait * (1 + random.uniform(-jitter, jitter))
                    _log.warning(
                        "%s: попытка %d/%d не удалась (%s) — повтор через %.2fс",
                        func.__qualname__, attempt, attempts, exc, sleep_for,
                    )
                    await asyncio.sleep(sleep_for)
                    wait *= backoff
            if reraise and last is not None:
                raise last
            _log.warning("%s: исчерпаны попытки (%s)", func.__qualname__, last)
            return default

        return wrapper

    return decorator


async def gather_safe(
    coros: Iterable[Awaitable[Any]],
    *,
    logger: Optional[logging.Logger] = None,
    label: str = "task",
) -> list:
    """
    ``asyncio.gather`` без падения на первой же ошибке.

    Возвращает список, где вместо упавшей задачи стоит ``None``.
    """
    _log = logger or log
    results = await asyncio.gather(*coros, return_exceptions=True)
    out = []
    for i, res in enumerate(results):
        if isinstance(res, BaseException):
            _log.debug("%s #%d завершился ошибкой: %s", label, i, res)
            out.append(None)
        else:
            out.append(res)
    return out


class SemaphoreLimiter:
    """
    Ограничитель конкурентности + минимальный интервал между запросами.

    ccxt имеет ``enableRateLimit``, но он не защищает от того, что мы сами
    одновременно выпустим 300 запросов к одной бирже.
    """

    def __init__(self, concurrency: int = 8, min_interval: float = 0.0) -> None:
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self):
        await self._sem.acquire()
        if self._min_interval:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                wait = self._last + self._min_interval - now
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = asyncio.get_running_loop().time()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._sem.release()
