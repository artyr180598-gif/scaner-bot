"""
app/data/exchanges.py — шлюзы к биржам через ccxt (async).

Один класс ``ExchangeGateway`` на биржу: тикеры, свечи, funding, open interest.
Все сетевые ошибки превращаются в ``DataError``/``None`` — вышестоящий сервис
решает, деградировать или переключиться на другую биржу. Ключей API не нужно: всё
публичное.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Sequence

import ccxt.async_support as ccxt_async

from app.config.settings import Settings
from app.domain.models import Candles, TickerInfo, Timeframe
from app.utils.errors import DataError, ExchangeUnavailable, SemaphoreLimiter
from app.utils.errors import retry_async

log = logging.getLogger(__name__)

# Биржи, у которых есть публичные данные по деривативам без ключей.
DERIVATIVES_OK = {"binance", "bybit", "okx", "kucoinfutures", "gate", "mexc", "bitget"}


class ExchangeGateway:
    """Тонкая обёртка над ccxt с ретраями, лимитом конкурентности и здоровьем."""

    def __init__(self, name: str, settings: Settings,
                 limiter: Optional[SemaphoreLimiter] = None) -> None:
        self.name = name.lower()
        self.settings = settings
        self._limiter = limiter or SemaphoreLimiter(
            settings.rate_limit_concurrency, settings.rate_limit_min_interval)
        klass = getattr(ccxt_async, self.name, None)
        if klass is None:
            raise DataError(f"Биржа {name!r} не поддерживается ccxt")
        self._ex = klass({
            "enableRateLimit": True,
            "timeout": settings.request_timeout_ms,
            "options": {"defaultType": settings.market_type},
        })
        self._markets_loaded = False
        self.errors = 0
        self.last_error: str = ""
        self.last_ok: float = 0.0

    # -- жизненный цикл -----------------------------------------------------
    async def close(self) -> None:
        try:
            await self._ex.close()
        except Exception:  # noqa: BLE001
            pass

    async def load_markets(self, reload: bool = False) -> Dict[str, Any]:
        async with self._limiter:
            try:
                markets = await self._ex.load_markets(reload)
                self._markets_loaded = True
                self._touch_ok()
                return markets
            except Exception as exc:  # noqa: BLE001
                self._touch_error(exc)
                raise DataError(f"{self.name}: не удалось загрузить рынки: {exc}") from exc

    @property
    def healthy(self) -> bool:
        return self.errors < 5 and (time.monotonic() - self.last_ok) < 600

    def _touch_ok(self) -> None:
        self.errors = 0
        self.last_error = ""
        self.last_ok = time.monotonic()

    def _touch_error(self, exc: BaseException) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]

    # -- данные -------------------------------------------------------------
    async def fetch_tickers(self, symbols: Optional[Sequence[str]] = None
                            ) -> Dict[str, Any]:
        async with self._limiter:
            try:
                data = await self._ex.fetch_tickers(list(symbols) if symbols else None)
                self._touch_ok()
                return data
            except Exception as exc:  # noqa: BLE001
                self._touch_error(exc)
                raise DataError(f"{self.name}: fetch_tickers: {exc}") from exc

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 500
                          ) -> List[List[float]]:
        async with self._limiter:
            try:
                rows = await self._ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                self._touch_ok()
                return rows or []
            except Exception as exc:  # noqa: BLE001
                self._touch_error(exc)
                raise DataError(f"{self.name}: fetch_ohlcv({symbol} {timeframe}): {exc}") from exc

    async def fetch_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        if self.name not in DERIVATIVES_OK:
            return None
        async with self._limiter:
            try:
                data = await self._ex.fetch_funding_rate(symbol)
                self._touch_ok()
                return data
            except Exception as exc:  # noqa: BLE001
                self._touch_error(exc)
                log.debug("%s: funding недоступен для %s (%s)", self.name, symbol, exc)
                return None

    async def fetch_open_interest(self, symbol: str) -> Optional[Dict[str, Any]]:
        if self.name not in DERIVATIVES_OK:
            return None
        async with self._limiter:
            try:
                data = await self._ex.fetch_open_interest(symbol)
                self._touch_ok()
                return data
            except Exception as exc:  # noqa: BLE001
                self._touch_error(exc)
                log.debug("%s: OI недоступен для %s (%s)", self.name, symbol, exc)
                return None

    async def fetch_open_interest_history(self, symbol: str, timeframe: str = "1h",
                                          limit: int = 26
                                          ) -> List[Dict[str, Any]]:
        if self.name not in DERIVATIVES_OK:
            return []
        async with self._limiter:
            try:
                data = await self._ex.fetch_open_interest_history(
                    symbol, timeframe=timeframe, limit=limit)
                self._touch_ok()
                return data or []
            except Exception as exc:  # noqa: BLE001
                self._touch_error(exc)
                log.debug("%s: история OI недоступна для %s (%s)", self.name, symbol, exc)
                return []

    async def fetch_order_book(self, symbol: str, limit: int = 20
                               ) -> Optional[Dict[str, Any]]:
        async with self._limiter:
            try:
                data = await self._ex.fetch_order_book(symbol, limit=limit)
                self._touch_ok()
                return data
            except Exception as exc:  # noqa: BLE001
                self._touch_error(exc)
                return None

    # -- служебное ----------------------------------------------------------
    def swap_symbol(self, base: str, quote: str) -> str:
        """Унифицированный символ перпетуала в ccxt: 'BTC/USDT:USDT'."""
        return f"{base}/{quote}:{quote}"

    def describe(self) -> str:
        state = "здоров" if self.healthy else "проблемы"
        extra = f" (последняя ошибка: {self.last_error})" if self.last_error else ""
        return f"{self.name}: {state}{extra}"


async def create_gateways(names: Sequence[str], settings: Settings,
                          limiter: Optional[SemaphoreLimiter] = None
                          ) -> List[ExchangeGateway]:
    """Создаёт шлюзы и грузит рынки у тех, что ответили."""
    gateways: List[ExchangeGateway] = []
    for name in names:
        try:
            gw = ExchangeGateway(name, settings, limiter)
        except DataError as exc:
            log.warning("пропускаем биржу %s: %s", name, exc)
            continue
        gateways.append(gw)
    if not gateways:
        raise ExchangeUnavailable("ни одна биржа из списка не поддерживается ccxt")

    results = await asyncio.gather(*(gw.load_markets() for gw in gateways),
                                   return_exceptions=True)
    alive = [gw for gw, res in zip(gateways, results) if not isinstance(res, BaseException)]
    for gw, res in zip(gateways, results):
        if isinstance(res, BaseException):
            log.warning("биржа %s недоступна при старте: %s", gw.name, res)
    if not alive:
        for gw in gateways:
            await gw.close()
        raise ExchangeUnavailable("все биржи недоступны: " +
                                  ", ".join(gw.last_error or gw.name for gw in gateways))
    return alive
