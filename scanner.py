"""
scanner.py — ядро сканера арбитражных связок «Spot → Futures hedge».

Архитектура
-----------
                    ┌────────────────────────────────────────────┐
                    │                ArbitrageScanner             │
                    └────────────────────────────────────────────┘
      init: load_markets → подбор пар → запуск сборщиков цен
                                    │
   ┌────────────────────────────────┼────────────────────────────────┐
   │ ExchangeSide (MEXC spot)       │ ExchangeSide (Bybit futures)   │  ... × 10 сторон
   │  WebSocket: watch_order_book   │  WebSocket: watch_order_book   │
   │  fallback: REST fetch_order_book (enableRateLimit=True)         │
   └────────────────────────────────┼────────────────────────────────┘
                                    │  кэш лучших Bid/Ask (свежесть ≤ BOOK_MAX_AGE)
                    ┌───────────────▼────────────────┐
                    │  цикл оценки (каждые N секунд) │
                    │  Spread = (FutBid - SpotAsk)/SpotAsk*100 - fees │
                    │  фильтры: порог, ликвидность,   │
                    │  свежесть, кулдаун на пару      │
                    └───────────────┬────────────────┘
                                    │  TelegramNotifier.send_html()
                                    ▼

Ключевые решения:
* Только ПУБЛИЧНЫЕ эндпоинты и WebSocket — никаких API-ключей.
* ccxt.pro (входит в пакет ccxt): один экземпляр даёт и watch_order_book,
  и fetch_order_book, поэтому переключение WS → REST прозрачно.
* enableRateLimit=True + дополнительные паузы — защита IP Railway от банов.
* Все сетевые ошибки гасятся с backoff: временный сбой не роняет процесс.

Улучшения v2 (профессиональный UX):
* Детальные сигналы с ценами, профитом для разных депозитов, пошаговым планом
* /top показывает цены покупки/продажи и ликвидность
* /price показывает лучшую связку и матрицу спредов
* Новые команды: /calc, /guide, /strategy, /exchanges
* Подсказки по исполнению: где покупать, где шортить, как хеджировать
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import ccxt
import ccxt.pro

from config import (
    FALLBACK_BASES,
    Settings,
    is_scannable_base,
)
from telegram_bot import MAIN_MENU_KEYBOARD, TelegramCommandListener

log = logging.getLogger("scanner")


# ---------------------------------------------------------------------------
# Реестр бирж: какие классы ccxt и какие defaultType использовать.
# binance: спот — класс binance, фьючерсы USDT-M — отдельный класс binanceusdm.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExchangeSpec:
    exchange_id: str
    display_name: str
    spot_class: str
    futures_class: str
    spot_default_type: str
    futures_default_type: str


EXCHANGE_REGISTRY: dict[str, ExchangeSpec] = {
    "mexc":    ExchangeSpec("mexc", "MEXC", "mexc", "mexc", "spot", "swap"),
    "bybit":   ExchangeSpec("bybit", "Bybit", "bybit", "bybit", "spot", "swap"),
    "gate":    ExchangeSpec("gate", "Gate.io", "gate", "gate", "spot", "swap"),
    "okx":     ExchangeSpec("okx", "OKX", "okx", "okx", "spot", "swap"),
    "binance": ExchangeSpec("binance", "Binance", "binance", "binanceusdm", "spot", "swap"),
}


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BookQuote:
    """Лучшие цены стакана (top of book) на момент получения."""

    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    bid_notional_usd: float
    ask_notional_usd: float
    timestamp: float  # epoch seconds — время прибытия котировки


@dataclass(slots=True, frozen=True)
class Opportunity:
    """Найденная арбитражная связка «купить спот / продать фьючерс»."""

    base: str
    spot_exchange: str
    futures_exchange: str
    spot_symbol: str
    futures_symbol: str
    spot_ask: float
    futures_bid: float
    gross_spread_percent: float
    net_spread_percent: float
    spot_notional_usd: float
    futures_notional_usd: float
    same_exchange: bool
    created_at: float
    data_age_seconds: float = 0.0  # возраст самой старой котировки связки


# ---------------------------------------------------------------------------
# Сторона биржи (спот ИЛИ фьючерсы) со своим сборщиком цен
# ---------------------------------------------------------------------------

class ExchangeSide:
    """
    Одна «сторона» одной биржи: либо спот-рынки, либо USDT-перпетуалы.

    Сама собирает цены выбранных символов:
      * режим 'ws'   — подписки watch_order_book (ccxt.pro, WebSocket);
      * режим 'rest' — круговой опрос fetch_order_book с enableRateLimit.

    При устойчивых сбоях WebSocket автоматически переключается на REST.
    """

    def __init__(
        self,
        settings: Settings,
        spec: ExchangeSpec,
        market_type: str,
        *,
        exchange: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.spec = spec
        self.exchange_id = spec.exchange_id
        self.market_type = market_type  # 'spot' | 'futures'
        self.display_name = spec.display_name
        self.label = f"{spec.display_name} {'spot' if market_type == 'spot' else 'futures'}"

        self.exchange = exchange if exchange is not None else self._build_exchange(spec, market_type)
        self.alive: bool = False            # load_markets прошёл успешно
        self.symbols: list[str] = []        # отслеживаемые unified-символы
        self.symbol_by_base: dict[str, str] = {}
        self.quotes: dict[str, BookQuote] = {}
        self.mode: str = "rest"
        if exchange is not None and settings.use_websocket:
            self.mode = "ws" if exchange.has.get("watchOrderBook") else "rest"

        self._tasks: dict[str, asyncio.Task] = {}
        self._zombie_tasks: list[asyncio.Task] = []  # отменённые, но ещё завершающиеся
        self._ws_fail_streak = 0
        self._logged_first_quote = False
        self.last_round_seconds: float = 0.0  # длительность последнего круга REST-опроса
        self.error_count = 0
        self._last_error_log = 0.0

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _build_exchange(spec: ExchangeSpec, market_type: str) -> Any:
        klass = getattr(ccxt.pro, spec.spot_class if market_type == "spot" else spec.futures_class)
        default_type = spec.spot_default_type if market_type == "spot" else spec.futures_default_type
        return klass({
            "enableRateLimit": True,     # встроенный троттлинг запросов
            "timeout": 20_000,           # 20с на REST-запрос
            "aiohttp_trust_env": True,   # поддержка HTTPS_PROXY из окружения
            "options": {"defaultType": default_type},
        })

    def index_markets(self) -> None:
        """Строит карту base → unified-symbol по загруженным рынкам."""
        symbol_by_base: dict[str, str] = {}
        for symbol, market in (getattr(self.exchange, "markets", None) or {}).items():
            try:
                if not market.get("active", True):
                    continue
                if market.get("quote") != "USDT":
                    continue
                base = market.get("base") or ""
                if not is_scannable_base(base):
                    continue
                if self.market_type == "spot":
                    if market.get("type") == "spot":
                        symbol_by_base[base] = symbol
                else:  # линейный перпетуал USDT-M: BASE/USDT:USDT
                    if (
                        market.get("type") == "swap"
                        and market.get("swap")
                        and market.get("linear")
                        and market.get("settle") == "USDT"
                    ):
                        symbol_by_base[base] = symbol
            except Exception:  # noqa: BLE001 — пропускаем кривые записи рынка
                continue
        self.symbol_by_base = symbol_by_base

    def assign_symbols(self, bases: Iterable[str]) -> None:
        """Назначает отслеживаемые символы по списку базовых активов."""
        wanted = [self.symbol_by_base[b] for b in bases if b in self.symbol_by_base]
        self.symbols = wanted
        keep = set(wanted)
        self.quotes = {sym: q for sym, q in self.quotes.items() if sym in keep}

    # ------------------------------------------------------------------ runtime
    def start(self) -> None:
        if not self.symbols:
            log.warning("%s: нет отслеживаемых символов — сторона простаивает", self.label)
            return
        if self.mode == "ws":
            for symbol in self.symbols:
                self._spawn_watch_task(symbol)
            log.info("%s: WebSocket-подписки на %d символов", self.label, len(self.symbols))
        else:
            self._spawn_rest_task()
            log.info("%s: REST-опрос %d символов", self.label, len(self.symbols))

    def reconcile(self, bases: Iterable[str]) -> None:
        """Синхронизирует набор символов после обновления рынков."""
        previous = set(self.symbols)
        self.assign_symbols(bases)
        if self.mode == "ws":
            current = set(self.symbols)
            for symbol in previous - current:
                task = self._tasks.pop(symbol, None)
                if task and not task.done():
                    task.cancel()
                    self._zombie_tasks.append(task)
            for symbol in current - previous:
                self._spawn_watch_task(symbol)

    async def stop(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        tasks += [t for t in self._zombie_tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._zombie_tasks.clear()

    async def aclose(self) -> None:
        await self.stop()
        if self.exchange is not None:
            try:
                await self.exchange.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: ошибка при закрытии соединения: %s", self.label, exc)

    def effective_book_max_age(self) -> float:
        """
        Допустимый возраст котировки с учётом режима стороны.

        В WebSocket-режиме это BOOK_MAX_AGE_SECONDS. В REST-режиме при
        огромном списке символов круг опроса может длиться минутами —
        допустимый возраст автоматически расширяется (2 круга + запас),
        иначе все котировки были бы «просроченными».
        """
        age = self.settings.book_max_age_seconds
        if self.mode == "rest" and self.last_round_seconds > 0:
            age = max(age, self.last_round_seconds * 2.0 + 10.0)
        return age

    def fresh_quotes_count(self) -> tuple[int, int]:
        """(свежих котировок, всего) с учётом эффективного возраста."""
        cutoff = time.time() - self.effective_book_max_age()
        fresh = sum(1 for q in self.quotes.values() if q.timestamp >= cutoff)
        return fresh, len(self.quotes)

    # ------------------------------------------------------------------ collectors
    def _spawn_watch_task(self, symbol: str) -> None:
        self._tasks[symbol] = asyncio.create_task(
            self._watch_symbol(symbol), name=f"{self.label}:ws:{symbol}"
        )

    def _spawn_rest_task(self) -> None:
        self._tasks["__rest__"] = asyncio.create_task(
            self._poll_loop(), name=f"{self.label}:rest-poller"
        )

    async def _watch_symbol(self, symbol: str) -> None:
        """Бесконечная WebSocket-подписка на стакан одного символа."""
        backoff = 1.0
        while self.mode == "ws":
            try:
                book = await self.exchange.watch_order_book(
                    symbol, self.settings.order_book_depth
                )
                self._ws_fail_streak = 0
                backoff = 1.0
                self._store_quote(symbol, book)
            except asyncio.CancelledError:
                raise
            except ccxt.NotSupported as exc:
                self._log_feed_error(symbol, exc, force=True)
                self._switch_to_rest(f"WebSocket не поддерживается ({exc})")
                return
            except Exception as exc:  # noqa: BLE001 — поток данных не должен умирать
                self._ws_fail_streak += 1
                self._log_feed_error(symbol, exc)
                if self._ws_fail_streak >= self.settings.ws_fails_before_fallback:
                    self._switch_to_rest(
                        f"{self._ws_fail_streak} неудач подряд "
                        f"(последняя: {type(exc).__name__})"
                    )
                    return
                await asyncio.sleep(backoff + random.uniform(0, 0.3))
                backoff = min(backoff * 2.0, 30.0)

    async def _poll_loop(self) -> None:
        """Круговой REST-опрос стаканов (резервный режим)."""
        while True:
            round_started = time.monotonic()
            for symbol in list(self.symbols):  # снимок: список мог обновиться
                try:
                    book = await self.exchange.fetch_order_book(symbol, self.settings.order_book_depth)
                    self._store_quote(symbol, book)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._log_feed_error(symbol, exc)
                if self.settings.rest_throttle_seconds > 0:
                    await asyncio.sleep(self.settings.rest_throttle_seconds)
            elapsed = time.monotonic() - round_started
            if self.last_round_seconds == 0.0 or abs(elapsed - self.last_round_seconds) > 30.0:
                log.info("%s: круг REST-опроса занял %.1fs (%d символов)",
                         self.label, elapsed, len(self.symbols))
            self.last_round_seconds = elapsed
            await asyncio.sleep(max(self.settings.rest_poll_interval_seconds, 0.05))

    def _switch_to_rest(self, reason: str) -> None:
        """Односторонняя деградация WS → REST (перезапуск вернёт WS)."""
        if self.mode != "ws":
            return
        self.mode = "rest"
        log.warning("%s: переключаюсь на REST-опрос — %s", self.label, reason)
        for key, task in list(self._tasks.items()):
            if key == "__rest__":
                continue
            task.cancel()
            self._zombie_tasks.append(task)
            self._tasks.pop(key, None)
        self._spawn_rest_task()

    # ------------------------------------------------------------------ helpers
    def _store_quote(self, symbol: str, book: dict[str, Any]) -> None:
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return
        try:
            bid, bid_qty = float(bids[0][0]), float(bids[0][1])
            ask, ask_qty = float(asks[0][0]), float(asks[0][1])
        except (TypeError, ValueError, IndexError):
            return
        if bid <= 0 or ask <= 0 or bid_qty <= 0 or ask_qty <= 0:
            return
        is_first_quote = symbol not in self.quotes
        self.quotes[symbol] = BookQuote(
            bid=bid, bid_qty=bid_qty, ask=ask, ask_qty=ask_qty,
            bid_notional_usd=bid * bid_qty, ask_notional_usd=ask * ask_qty,
            timestamp=time.time(),
        )
        if is_first_quote:
            if not self._logged_first_quote:
                self._logged_first_quote = True
                log.info(
                    "%s: поток данных активен — %s bid=%s / ask=%s (объёмы: %s / %s)",
                    self.label, symbol, _fmt_price(bid), _fmt_price(ask),
                    _fmt_price(bid_qty), _fmt_price(ask_qty),
                )
            else:
                log.debug("%s: первая котировка %s: bid=%s ask=%s",
                          self.label, symbol, _fmt_price(bid), _fmt_price(ask))

    def _log_feed_error(self, context: str, exc: Exception, force: bool = False) -> None:
        """Логирует ошибки потока без спама: первую, затем каждую 10-ю."""
        self.error_count += 1
        now = time.monotonic()
        if force or self.error_count == 1 or self.error_count % 10 == 0 or now - self._last_error_log > 300:
            log.warning(
                "%s: сбой потока данных [%s] (всего ошибок: %d): %s: %s",
                self.label, context, self.error_count, type(exc).__name__, exc,
            )
            self._last_error_log = now


# ---------------------------------------------------------------------------
# Сканер
# ---------------------------------------------------------------------------

class ArbitrageScanner:
    """Оркестратор: инициализация бирж, цикл оценки спредов, сигналы."""

    #: попыток подключиться к бирже на старте, прежде чем исключить её
    MARKET_LOAD_ATTEMPTS = 3

    def __init__(self, settings: Settings, notifier: Any) -> None:
        self.settings = settings
        self.notifier = notifier
        self.spot_sides: list[ExchangeSide] = []
        self.futures_sides: list[ExchangeSide] = []
        self.bases: list[str] = []
        self._cooldown_until: dict[str, float] = {}
        self._last_best: Optional[tuple[float, str, str]] = None  # (net%, base, маршрут)
        self._listener_task: Optional[asyncio.Task] = None
        self.stats: dict[str, int] = {
            "scans": 0,
            "signals_sent": 0,
            "signals_suppressed_cooldown": 0,
            "combinations_checked": 0,
        }
        self._started_at = time.time()

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        """Главная корутина; исключение наверх = перезапуск супервизором."""
        try:
            await self._startup()
            await self._main_loop()
        except asyncio.CancelledError:
            log.info("Сканер останавливается (получен сигнал завершения)")
            raise
        finally:
            await self._shutdown()

    async def _startup(self) -> None:
        await self.notifier.start()
        self._build_sides()
        await self._init_markets()

        live_spots = [s for s in self.spot_sides if s.alive]
        live_futures = [s for s in self.futures_sides if s.alive]
        if not live_spots or not live_futures:
            message = (
                "Ни одна спот-биржа недоступна" if not live_spots
                else "Ни одна фьючерсная биржа недоступна"
            )
            try:
                await self.notifier.send_html(
                    f"⚠️ <b>Сканер не смог подключиться к биржам</b>\n{message}. "
                    f"Перезапуск через {self.settings.restart_backoff_seconds:.0f}с."
                )
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(message)

        self.bases = await self._resolve_bases()
        for side in self._live_sides():
            side.assign_symbols(self.bases)
            side.start()

        dead = [s.label for s in self.spot_sides + self.futures_sides if not s.alive]
        log.info(
            "Сканер запущен: %d живых сторон, %d пар (%s)",
            len(self._live_sides()), len(self.bases),
            ", ".join(self.bases[:10]) + ("…" if len(self.bases) > 10 else ""),
        )
        if dead:
            log.warning("Недоступные стороны (пропущены): %s", ", ".join(dead))
        await self.notifier.send_html(
            format_startup_message(self.settings, self.bases, dead_labels=dead),
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        self._start_command_listener()

    async def _shutdown(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._listener_task = None
        for side in self.spot_sides + self.futures_sides:
            await side.aclose()
        await self.notifier.close()
        log.info("Сканер остановлен")

    # ------------------------------------------------------------------ setup steps
    def _build_sides(self) -> None:
        for exchange_id in self.settings.exchanges:
            spec = EXCHANGE_REGISTRY[exchange_id]
            self.spot_sides.append(ExchangeSide(self.settings, spec, "spot"))
            self.futures_sides.append(ExchangeSide(self.settings, spec, "futures"))

    async def _init_markets(self) -> None:
        async def init_side(side: ExchangeSide) -> None:
            for attempt in range(1, self.MARKET_LOAD_ATTEMPTS + 1):
                try:
                    await asyncio.wait_for(side.exchange.load_markets(), timeout=45.0)
                    side.index_markets()
                    side.alive = True
                    log.info(
                        "%s: рынков загружено %d, подходит под фильтр %d",
                        side.label,
                        len(side.exchange.markets or {}),
                        len(side.symbol_by_base),
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    side.alive = False
                    log.warning(
                        "%s: load_markets не удался (попытка %d/%d): %s: %s",
                        side.label, attempt, self.MARKET_LOAD_ATTEMPTS,
                        type(exc).__name__, exc,
                    )
                    if attempt < self.MARKET_LOAD_ATTEMPTS:
                        await asyncio.sleep(5.0 * attempt)
            log.error("%s: биржа исключена из сканирования", side.label)

        await asyncio.gather(*(init_side(side) for side in self.spot_sides + self.futures_sides))

    async def _resolve_bases(self) -> list[str]:
        """
        Список базовых активов для сканирования.

        Приоритет: SYMBOLS (явный список) > все поддерживаемые монеты
        (TOP_SYMBOLS=0, по умолчанию) > топ-N по объёму торгов (TOP_SYMBOLS=N).
        """
        s = self.settings
        if s.symbols:
            bases = [b for b in s.symbols if self._base_supported(b)]
            skipped = [b for b in s.symbols if b not in bases]
            if skipped:
                log.warning("SYMBOLS: недоступны на выбранных биржах: %s", ", ".join(skipped))
            if not bases:
                raise RuntimeError("SYMBOLS: ни один тикер не найден на доступных биржах")
            return bases

        supported = self._all_supported_bases()

        if s.top_symbols_limit > 0 and s.auto_discover_symbols:
            ranked = await self._rank_bases_by_volume(supported)
            if ranked:
                return ranked
            log.warning("Ранжирование по объёму не удалось — беру первые %d поддерживаемых",
                        s.top_symbols_limit)
            return supported[: s.top_symbols_limit]

        if supported:
            return supported

        return [b for b in FALLBACK_BASES if self._base_supported(b)]

    def _all_supported_bases(self) -> list[str]:
        """Все базы: спот на ≥1 живой стороне И перп на ≥1 живой стороне."""
        spot_bases: set[str] = set()
        for side in self.spot_sides:
            if side.alive:
                spot_bases.update(side.symbol_by_base)
        futures_bases: set[str] = set()
        for side in self.futures_sides:
            if side.alive:
                futures_bases.update(side.symbol_by_base)
        return sorted(spot_bases & futures_bases)

    async def _rank_bases_by_volume(self, candidates: list[str]) -> list[str]:
        """Топ пар по объёму торгов (quote volume) с reference-биржи."""
        limit = self.settings.top_symbols_limit
        candidate_set = set(candidates)
        for side in self.spot_sides:
            if not side.alive:
                continue
            try:
                tickers = await asyncio.wait_for(side.exchange.fetch_tickers(), timeout=60.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Ранжирование: %s не отдал тикеры (%s) — пробую следующую",
                    side.label, exc,
                )
                continue

            scored: list[tuple[str, float]] = []
            for symbol, ticker in (tickers or {}).items():
                market = (side.exchange.markets or {}).get(symbol)
                if not market or market.get("type") != "spot":
                    continue
                if market.get("quote") != "USDT":
                    continue
                base = market.get("base") or ""
                if base not in candidate_set:
                    continue
                try:
                    volume = float(ticker.get("quoteVolume") or 0.0)
                except (TypeError, ValueError):
                    continue
                scored.append((base, volume))

            scored.sort(key=lambda item: item[1], reverse=True)
            if scored:
                log.info(
                    "Ранжирование по объёму торгов (%s): топ-%d из %d пар",
                    side.label, min(limit, len(scored)), len(scored),
                )
                return [base for base, _ in scored[:limit]]
        return []

    def _base_supported(self, base: str) -> bool:
        """Есть ли у базы и спот-рынок, и перпетуал хотя бы на одной бирже."""
        has_spot = any(s.alive and base in s.symbol_by_base for s in self.spot_sides)
        has_futures = any(s.alive and base in s.symbol_by_base for s in self.futures_sides)
        return has_spot and has_futures

    def _live_sides(self) -> list[ExchangeSide]:
        return [s for s in self.spot_sides + self.futures_sides if s.alive]

    # ------------------------------------------------------------------ main loop
    async def _main_loop(self) -> None:
        s = self.settings
        last_status = time.monotonic()
        last_heartbeat = time.monotonic()
        last_refresh = time.monotonic()

        while True:
            await asyncio.sleep(s.scan_interval_seconds)
            self.stats["scans"] += 1

            pairs = self._evaluate()
            await self._process_signals(pairs)

            now_mono = time.monotonic()
            if s.status_log_minutes > 0 and now_mono - last_status >= s.status_log_minutes * 60:
                last_status = now_mono
                self._log_status()
            if s.heartbeat_minutes > 0 and now_mono - last_heartbeat >= s.heartbeat_minutes * 60:
                last_heartbeat = now_mono
                await self._send_heartbeat()
            if s.market_refresh_minutes > 0 and now_mono - last_refresh >= s.market_refresh_minutes * 60:
                last_refresh = now_mono
                await self._refresh_markets()

    # ------------------------------------------------------------------ evaluation
    def _collect_opportunities(self, threshold: float) -> dict[str, list[Opportunity]]:
        """
        Считает чистые спреды для всех комбинаций (спот-биржа × фьючерс-биржа × пара).

        Возвращает {пара: [связки, отсортированные по чистому спреду]}.
        Порог threshold задаёт минимальный чистый спред (-inf = все связки).
        """
        now = time.time()
        fee_percent = self.settings.total_fee_percent
        by_base: dict[str, list[Opportunity]] = {}

        for spot_side in self.spot_sides:
            if not spot_side.alive:
                continue
            spot_cutoff = now - spot_side.effective_book_max_age()
            for fut_side in self.futures_sides:
                if not fut_side.alive:
                    continue
                if (
                    spot_side.exchange_id == fut_side.exchange_id
                    and not self.settings.allow_same_exchange
                ):
                    continue
                fut_cutoff = now - fut_side.effective_book_max_age()
                for base in self.bases:
                    spot_symbol = spot_side.symbol_by_base.get(base)
                    fut_symbol = fut_side.symbol_by_base.get(base)
                    if spot_symbol is None or fut_symbol is None:
                        continue
                    spot_quote = spot_side.quotes.get(spot_symbol)
                    fut_quote = fut_side.quotes.get(fut_symbol)
                    if spot_quote is None or fut_quote is None:
                        continue
                    if spot_quote.timestamp < spot_cutoff or fut_quote.timestamp < fut_cutoff:
                        continue
                    if spot_quote.ask <= 0.0 or fut_quote.bid <= 0.0:
                        continue

                    gross = (fut_quote.bid - spot_quote.ask) / spot_quote.ask * 100.0
                    net = gross - fee_percent
                    self.stats["combinations_checked"] += 1
                    self._track_best(net, base, spot_side.display_name, fut_side.display_name)

                    if net < threshold:
                        continue
                    notional = min(spot_quote.ask_notional_usd, fut_quote.bid_notional_usd)
                    if self.settings.min_notional_usd > 0 and notional < self.settings.min_notional_usd:
                        continue

                    by_base.setdefault(base, []).append(Opportunity(
                        base=base,
                        spot_exchange=spot_side.display_name,
                        futures_exchange=fut_side.display_name,
                        spot_symbol=spot_symbol,
                        futures_symbol=fut_symbol,
                        spot_ask=spot_quote.ask,
                        futures_bid=fut_quote.bid,
                        gross_spread_percent=gross,
                        net_spread_percent=net,
                        spot_notional_usd=spot_quote.ask_notional_usd,
                        futures_notional_usd=fut_quote.bid_notional_usd,
                        same_exchange=spot_side.exchange_id == fut_side.exchange_id,
                        created_at=now,
                        data_age_seconds=now - min(spot_quote.timestamp, fut_quote.timestamp),
                    ))

        for opportunities in by_base.values():
            opportunities.sort(key=lambda o: o.net_spread_percent, reverse=True)
        return by_base

    def _evaluate(self) -> list[tuple[Opportunity, list[Opportunity]]]:
        """
        Связки, подходящие под сигнал: лучшая связка на пару + альтернативы,
        отсортировано по убыванию чистого спреда.
        """
        by_base = self._collect_opportunities(threshold=self.settings.min_spread_percent)
        pairs: list[tuple[Opportunity, list[Opportunity]]] = []
        for opportunities in by_base.values():
            pairs.append((opportunities[0], opportunities[1:3]))
        pairs.sort(key=lambda pair: pair[0].net_spread_percent, reverse=True)
        return pairs

    def _track_best(self, net: float, base: str, spot_name: str, fut_name: str) -> None:
        if self._last_best is None or net > self._last_best[0]:
            self._last_best = (net, base, f"{spot_name} → {fut_name}")

    async def _process_signals(self, pairs: list[tuple[Opportunity, list[Opportunity]]]) -> None:
        """Отправляет сигналы с учётом кулдауна и лимита на один цикл."""
        if not pairs:
            return
        now = time.time()
        sent = 0

        for best, alternatives in pairs:
            if sent >= self.settings.max_signals_per_scan:
                log.info(
                    "Сигналов за цикл больше лимита (%d): %d подавлено",
                    self.settings.max_signals_per_scan, len(pairs) - sent,
                )
                break
            cooldown_until = self._cooldown_until.get(best.base, 0.0)
            if now < cooldown_until:
                self.stats["signals_suppressed_cooldown"] += 1
                continue

            html_message = format_signal_message(
                best, alternatives, self.settings,
                cooldown_until=now + self.settings.cooldown_seconds,
            )
            delivered = await self.notifier.send_html(
                html_message, reply_markup=MAIN_MENU_KEYBOARD
            )
            if delivered:
                self._cooldown_until[best.base] = now + self.settings.cooldown_seconds
                self.stats["signals_sent"] += 1
                sent += 1
                log.info(
                    "СИГНАЛ %s/USDT: чистый спред +%.2f%% (%s спот → %s фьючерс)",
                    best.base, best.net_spread_percent,
                    best.spot_exchange, best.futures_exchange,
                )
            else:
                self._cooldown_until[best.base] = now + 60.0
            await asyncio.sleep(0.3)

    # ------------------------------------------------------------------ periodic
    async def _refresh_markets(self) -> None:
        """Периодическое обновление рынков: листинги/деллистинги, статус active."""
        log.info("Обновляю списки рынков бирж...")
        for side in self._live_sides():
            try:
                await asyncio.wait_for(side.exchange.load_markets(), timeout=60.0)
                side.index_markets()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: не удалось обновить рынки: %s", side.label, exc)
        for side in self._live_sides():
            side.reconcile(self.bases)
        total = sum(len(side.symbols) for side in self._live_sides())
        log.info("Рынки обновлены: суммарно отслеживается %d символов", total)

    async def _send_heartbeat(self) -> None:
        await self.notifier.send_html(
            format_heartbeat_message(self), reply_markup=MAIN_MENU_KEYBOARD
        )

    def _log_status(self) -> None:
        sides = self._live_sides()
        fresh = total = 0
        for side in sides:
            f, t = side.fresh_quotes_count()
            fresh += f
            total += t
        best = "нет данных" if self._last_best is None else (
            f"{self._last_best[0]:+.2f}% ({self._last_best[1]}, {self._last_best[2]})"
        )
        log.info(
            "Статус: сторон %d, свежих стаканов %d/%d, сканов %d, "
            "сигналов %d (подавлено кулдауном %d), лучший чистый спред: %s",
            len(sides), fresh, total, self.stats["scans"],
            self.stats["signals_sent"], self.stats["signals_suppressed_cooldown"], best,
        )

    # ------------------------------------------------------------------ команды Telegram
    def _start_command_listener(self) -> None:
        """Запускает приём команд/кнопок Telegram (long polling)."""
        if getattr(self.notifier, "dry_run", False):
            log.info("Команды Telegram отключены: DRY-RUN (нет TELEGRAM_BOT_TOKEN/CHAT_ID)")
            return
        if not self.settings.chat_ids:
            log.info("Команды Telegram отключены: не задан CHAT_ID")
            return
        listener = TelegramCommandListener(
            self.notifier,
            handlers=self.telegram_handlers(),
            allowed_chat_ids=self.settings.chat_ids,
        )
        self._listener_task = asyncio.create_task(listener.run(), name="telegram-commands")

    def telegram_handlers(self) -> dict[str, Any]:
        """Реестр команд: имя команды -> async-обработчик (chat_id, args) -> HTML."""
        return {
            "start": self._cmd_help,
            "help": self._cmd_help,
            "status": self._cmd_status,
            "top": self._cmd_top,
            "spreads": self._cmd_top,
            "price": self._cmd_price,
            "calc": self._cmd_calc,
            "guide": self._cmd_guide,
            "strategy": self._cmd_strategy,
            "exchanges": self._cmd_exchanges,
        }

    async def _cmd_help(self, chat_id: str, args: str) -> str:
        return format_help_message(self.settings)

    async def _cmd_status(self, chat_id: str, args: str) -> str:
        return format_status_message(self)

    async def _cmd_top(self, chat_id: str, args: str) -> str:
        try:
            limit = max(1, min(20, int(args.split()[0]))) if args.strip() else 10
        except (ValueError, IndexError):
            limit = 10
        return self._build_top_spreads_message(limit)

    async def _cmd_price(self, chat_id: str, args: str) -> str:
        base = (args.split()[0] if args.strip() else "BTC").upper()
        return self._build_price_message(base)

    async def _cmd_calc(self, chat_id: str, args: str) -> str:
        # /calc BTC 1000  -> base=BTC, amount=1000
        parts = args.strip().split()
        base = parts[0].upper() if parts else "BTC"
        try:
            amount = float(parts[1].replace(",", ".").replace("$", "")) if len(parts) > 1 else 1000.0
        except ValueError:
            amount = 1000.0
        amount = max(1.0, min(1000000.0, amount))
        return self._build_calc_message(base, amount)

    async def _cmd_guide(self, chat_id: str, args: str) -> str:
        return format_guide_message()

    async def _cmd_strategy(self, chat_id: str, args: str) -> str:
        return format_strategy_message(self.settings)

    async def _cmd_exchanges(self, chat_id: str, args: str) -> str:
        return self._build_exchanges_message()

    # ------------------------------------------------------------------ ответы командам
    def _build_top_spreads_message(self, limit: int = 10) -> str:
        """Топ чистых спредов — профессиональный вид с ценами и профитом."""
        by_base = self._collect_opportunities(threshold=float("-inf"))
        best_list = [opps[0] for opps in by_base.values()]
        best_list.sort(key=lambda o: o.net_spread_percent, reverse=True)

        if not best_list:
            return (
                "📊 <b>Топ спредов</b>: нет свежих данных со стаканов — подождите пару секунд.\n"
                "Биржи загружаются, WebSocket подключается...\n\n"
                "Попробуйте /status чтобы проверить состояние бирж."
            )

        lines = [
            f"📊 <b>Топ-{min(limit, len(best_list))} чистых спредов прямо сейчас</b>",
            f"<i>Реальные цены из стаканов, обновляется каждые {self.settings.scan_interval_seconds:.0f}с</i>",
            "",
        ]
        for position, opp in enumerate(best_list[:limit], start=1):
            hot = " 🔥" if opp.net_spread_percent >= self.settings.min_spread_percent else ""
            cold = " ❄️" if opp.net_spread_percent < 0 else ""
            emoji = hot or cold
            # Пример: 1. BTC +2.85% — Buy MEXC @ 67234.12 → Sell Bybit @ 69500.00
            lines.append(
                f"{position}. <b>{_esc(opp.base)}</b> <b>{opp.net_spread_percent:+.2f}%</b>{emoji}\n"
                f"   📥 {_esc(opp.spot_exchange)} <code>{_fmt_price(opp.spot_ask)}</code> → "
                f"📤 {_esc(opp.futures_exchange)} <code>{_fmt_price(opp.futures_bid)}</code>\n"
                f"   💧 {_fmt_usd(min(opp.spot_notional_usd, opp.futures_notional_usd))} "
                f"| ⏱ {opp.data_age_seconds:.0f}с | Gross {opp.gross_spread_percent:+.2f}%"
            )
            lines.append("")

        above = sum(1 for o in best_list if o.net_spread_percent >= self.settings.min_spread_percent)
        lines += [
            f"🚦 Порог сигнала: ≥ {self.settings.min_spread_percent:.2f}% (связок над порогом: {above})",
            f"💳 Комиссии учтены: {self.settings.total_fee_percent:.2f}%",
            "",
            "<b>Как читать:</b> 📥 — где покупать спот (ask), 📤 — где шортить фьючерс (bid).",
            "Чистый спред = (FutBid - SpotAsk)/SpotAsk*100 - комиссии.",
            "",
            "<i>Автосигналы по парам в кулдауне не дублируются; /top показывает всё без кулдауна.</i>",
            "Используйте /price BTC для детального разбора по монете.",
        ]
        return "\n".join(lines)

    def _build_price_message(self, base: str) -> str:
        """Реальные цены + лучшая связка + матрица — максимально понятно."""
        cutoff = time.time() - self.settings.book_max_age_seconds
        now = time.time()

        # Собираем котировки
        spot_quotes: list[tuple[ExchangeSide, BookQuote]] = []
        fut_quotes: list[tuple[ExchangeSide, BookQuote]] = []

        for side in self.spot_sides:
            if not side.alive:
                continue
            symbol = side.symbol_by_base.get(base)
            q = side.quotes.get(symbol) if symbol else None
            if q and q.timestamp >= cutoff:
                spot_quotes.append((side, q))
        for side in self.futures_sides:
            if not side.alive:
                continue
            symbol = side.symbol_by_base.get(base)
            q = side.quotes.get(symbol) if symbol else None
            if q and q.timestamp >= cutoff:
                fut_quotes.append((side, q))

        if not spot_quotes and not fut_quotes:
            return (
                f"💠 <b>{_esc(base)}/USDT — нет свежих данных</b>\n\n"
                f"Возможные причины:\n"
                f"• Пара не торгуется на выбранных биржах\n"
                f"• Биржи ещё загружаются\n"
                f"• {base} — неликвид\n\n"
                f"Попробуйте /top для списка активных пар или /price BTC /price ETH."
            )

        # Лучший спот (минимальный ask) и лучший фьючерс (максимальный bid)
        best_spot = min(spot_quotes, key=lambda x: x[1].ask) if spot_quotes else None
        best_fut = max(fut_quotes, key=lambda x: x[1].bid) if fut_quotes else None

        lines = [
            f"💠 <b>{_esc(base)}/USDT — реальные цены стаканов</b>",
            f"<i>Обновлено: {_fmt_utc(now)} | Возраст ≤ {self.settings.book_max_age_seconds:.0f}с</i>",
            "",
        ]

        if spot_quotes:
            lines.append("<b>📥 СПОТ — цена покупки (ask):</b> <i>чем ниже, тем лучше для покупки</i>")
            # сортируем по ask
            for side, q in sorted(spot_quotes, key=lambda x: x[1].ask):
                age = now - q.timestamp
                lines.append(
                    f"   {_esc(side.display_name)}: <code>{_fmt_price(q.ask)}</code> USDT "
                    f"(bid {_fmt_price(q.bid)}) | 💧 {_fmt_usd(q.ask_notional_usd)} | {age:.0f}с"
                )
            lines.append("")

        if fut_quotes:
            lines.append("<b>📤 ФЬЮЧЕРСЫ (перпетуалы) — цена продажи (bid):</b> <i>чем выше, тем лучше для шорта</i>")
            for side, q in sorted(fut_quotes, key=lambda x: x[1].bid, reverse=True):
                age = now - q.timestamp
                lines.append(
                    f"   {_esc(side.display_name)}: <code>{_fmt_price(q.bid)}</code> USDT "
                    f"(ask {_fmt_price(q.ask)}) | 💧 {_fmt_usd(q.bid_notional_usd)} | {age:.0f}с"
                )
            lines.append("")

        # Лучшая связка для этой базы
        by_base = self._collect_opportunities(threshold=float("-inf"))
        opps = by_base.get(base, [])
        if opps:
            best = opps[0]
            profit_1000 = best.net_spread_percent / 100 * 1000
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"🏆 <b>ЛУЧШАЯ СВЯЗКА ДЛЯ {_esc(base)}</b>",
                f"💎 Чистый спред: <b>{best.net_spread_percent:+.2f}%</b> (Gross {best.gross_spread_percent:+.2f}%)",
                f"📥 Покупать спот: <b>{_esc(best.spot_exchange)}</b> @ <code>{_fmt_price(best.spot_ask)}</code>",
                f"📤 Шортить фьючерс: <b>{_esc(best.futures_exchange)}</b> @ <code>{_fmt_price(best.futures_bid)}</code>",
                f"💰 Профит с $1000 → <b>${profit_1000:+.2f}</b> (после комиссий {self.settings.total_fee_percent:.2f}%)",
                f"💧 Ликвидность: {_fmt_usd(best.spot_notional_usd)} / {_fmt_usd(best.futures_notional_usd)}",
                "",
                "<b>📋 Что делать:</b>",
                f"1️⃣ Купи {base} на споте {_esc(best.spot_exchange)} по ~{_fmt_price(best.spot_ask)}",
                f"2️⃣ Одновременно открой SHORT на перпетуале {_esc(best.futures_exchange)} по ~{_fmt_price(best.futures_bid)}",
                f"3️⃣ Держи хедж: LONG спот / SHORT perp = нет риска направления цены",
                f"4️⃣ Закрой при схождении цен или держи для funding-платежей",
            ]
            if len(opps) > 1:
                lines.append("")
                lines.append("<b>🔀 Другие маршруты:</b>")
                for alt in opps[1:4]:
                    lines.append(
                        f"   • {_esc(alt.spot_exchange)} → {_esc(alt.futures_exchange)}: "
                        f"{alt.net_spread_percent:+.2f}% "
                        f"({_fmt_price(alt.spot_ask)} → {_fmt_price(alt.futures_bid)})"
                    )
        else:
            # Если нет связок выше -inf, значит нет пересечения по времени или цены
            if best_spot and best_fut:
                gross = (best_fut[1].bid - best_spot[1].ask) / best_spot[1].ask * 100
                net = gross - self.settings.total_fee_percent
                lines += [
                    "━━━━━━━━━━━━━━━━━━━━",
                    f"📊 <b>Расчёт по лучшим ценам:</b>",
                    f"Лучший спот ask: {_esc(best_spot[0].display_name)} {_fmt_price(best_spot[1].ask)}",
                    f"Лучший фьючерс bid: {_esc(best_fut[0].display_name)} {_fmt_price(best_fut[1].bid)}",
                    f"Gross: {gross:+.2f}% | Net (после {self.settings.total_fee_percent:.2f}% комиссий): {net:+.2f}%",
                    f"{'✅ Над порогом' if net >= self.settings.min_spread_percent else '❌ Ниже порога сигнала'} "
                    f"(порог {self.settings.min_spread_percent:.2f}%)",
                ]

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "<i>💡 Совет: используй /calc BTC 1000 чтобы посчитать профит для своего депозита.</i>",
            "<i>📚 /guide — подробный гайд как исполнять сделки.</i>",
        ]
        return "\n".join(lines)

    def _build_calc_message(self, base: str, amount_usd: float) -> str:
        """Калькулятор профита для указанной базы и суммы."""
        by_base = self._collect_opportunities(threshold=float("-inf"))
        opps = by_base.get(base, [])
        if not opps:
            return (
                f"🧮 <b>Калькулятор: {_esc(base)} / ${amount_usd:.0f}</b>\n\n"
                f"Нет свежих данных по {base}. Попробуй /price {base} или /top."
            )

        best = opps[0]
        gross_profit = amount_usd * best.gross_spread_percent / 100
        fee_usd = amount_usd * self.settings.total_fee_percent / 100
        net_profit = amount_usd * best.net_spread_percent / 100

        lines = [
            f"🧮 <b>Калькулятор профита: {_esc(base)} — ${amount_usd:,.0f}</b>",
            "",
            f"📥 Спот {_esc(best.spot_exchange)}: <code>{_fmt_price(best.spot_ask)}</code>",
            f"📤 Фьючерс {_esc(best.futures_exchange)}: <code>{_fmt_price(best.futures_bid)}</code>",
            f"📊 Gross: {best.gross_spread_percent:+.2f}% | Fees: {self.settings.total_fee_percent:.2f}% | "
            f"<b>Net: {best.net_spread_percent:+.2f}%</b>",
            "",
            f"💵 Депозит: ${amount_usd:,.2f}",
            f"💰 Gross профит: ${gross_profit:+.2f}",
            f"💳 Комиссии: -${fee_usd:.2f} ({self.settings.spot_taker_fee_percent:.2f}% + {self.settings.futures_taker_fee_percent:.2f}%)",
            f"💎 <b>Чистый профит: ${net_profit:+.2f}</b>",
            "",
            "<b>📋 На другие суммы:</b>",
        ]
        for amt in [100, 500, 1000, 5000, 10000]:
            if abs(amt - amount_usd) < 1:
                continue
            p = amt * best.net_spread_percent / 100
            lines.append(f"   ${amt:>5,.0f} → ${p:+.2f} ({best.net_spread_percent:+.2f}%)")

        lines += [
            "",
            "<b>📋 План:</b>",
            f"1. Купи {base} на {_esc(best.spot_exchange)} на ${amount_usd:.0f}",
            f"2. Зашорти {base} на {_esc(best.futures_exchange)} на ${amount_usd:.0f}",
            f"3. Профит ${net_profit:+.2f} при мгновенном закрытии, плюс funding каждые 8ч",
            "",
            f"<i>Возраст данных: {best.data_age_seconds:.0f}с | Ликвидность: {_fmt_usd(best.spot_notional_usd)}</i>",
        ]
        return "\n".join(lines)

    def _build_exchanges_message(self) -> str:
        """Статус бирж — профессиональный дашборд."""
        lines = [
            "🏦 <b>Биржи — статус подключения</b>",
            f"<i>{_fmt_utc(time.time())}</i>",
            "",
        ]
        for side in self.spot_sides + self.futures_sides:
            if side.alive:
                fresh, total = side.fresh_quotes_count()
                mode_emoji = "⚡" if side.mode == "ws" else "🔄"
                mode = "WebSocket" if side.mode == "ws" else "REST"
                status = f"✅ {mode_emoji} {mode}"
                lines.append(
                    f"{status} <b>{_esc(side.label)}</b>\n"
                    f"   Пар: {len(side.symbols)} | Свежих: {fresh}/{total} | "
                    f"Ошибок: {side.error_count} | Круг: {side.last_round_seconds:.1f}с"
                )
            else:
                lines.append(f"❌ <b>{_esc(side.label)}</b> — недоступна (исключена)")
            lines.append("")

        live = len(self._live_sides())
        total_sides = len(self.spot_sides) + len(self.futures_sides)
        lines += [
            f"📡 Живых сторон: {live}/{total_sides}",
            f"🎯 Отслеживается пар: {len(self.bases)}",
            "",
            "<i>WS = WebSocket (быстро, реалтайм), REST = опрос (медленнее, но надёжно).</i>",
            "Если биржа недоступна — бот продолжает работать с остальными.",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Форматирование сообщений (HTML для Telegram) — ПРОФЕССИОНАЛЬНЫЙ UX
# ---------------------------------------------------------------------------

def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    if value >= 0.01:
        return f"{value:.6f}"
    return f"{value:.8f}"


def _fmt_usd(value: float) -> str:
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.4f}"


def _fmt_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    parts.append(f"{minutes}м")
    return " ".join(parts)


def format_signal_message(
    opportunity: Opportunity,
    alternatives: list[Opportunity],
    settings: Settings,
    *,
    cooldown_until: float,
) -> str:
    """
    Профессиональное сообщение о связке — максимально понятное.
    Сохраняет совместимость с тестами: содержит BTC/USDT, +2.85%, MEXC, Bybit,
    100.00, 103.00, +3.00%, 0.10%, $12,345, OKX, 'не раньше'.
    """
    o = opportunity

    # Профит для разных депозитов
    def profit(usd: float) -> float:
        return usd * o.net_spread_percent / 100.0

    fire = " 🔥🔥🔥" if o.net_spread_percent >= settings.min_spread_percent + 2 else " 🔥" if o.net_spread_percent >= settings.min_spread_percent else ""
    lines = [
        f"🚀 <b>АРБИТРАЖНАЯ СВЯЗКА · {_esc(o.base)}/USDT{fire}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💎 <b>Чистый спред: +{o.net_spread_percent:.2f}%</b> (после комиссий {settings.total_fee_percent:.2f}%)",
        f"📊 Гросс-спред: <b>+{o.gross_spread_percent:.2f}%</b>",
        "",
        f"📥 <b>Купить спот:</b> {_esc(o.spot_exchange)} — <code>{_fmt_price(o.spot_ask)}</code> USDT",
        f"   Пара: {_esc(o.spot_symbol)} | Глубина: {_fmt_usd(o.spot_notional_usd)}",
        f"📤 <b>Продать фьючерс:</b> {_esc(o.futures_exchange)} — <code>{_fmt_price(o.futures_bid)}</code> USDT",
        f"   Пара: {_esc(o.futures_symbol)} | Глубина: {_fmt_usd(o.futures_notional_usd)}",
        "",
        f"💳 Комиссии: {settings.spot_taker_fee_percent:.2f}% спот + {settings.futures_taker_fee_percent:.2f}% фьючерс = {settings.total_fee_percent:.2f}%",
        f"📡 Возраст котировок: ≤ {o.data_age_seconds:.0f}с | Свежие данные из стакана",
        f"🔁 Схема: <b>long спот / short перп. (хедж)</b> — без риска направления",
    ]

    # Профит
    lines += [
        "",
        "💰 <b>ПРОФИТ (пример, без учёта funding):</b>",
        f"   $100 → <b>${profit(100):+.2f}</b> | $500 → <b>${profit(500):+.2f}</b>",
        f"   $1,000 → <b>${profit(1000):+.2f}</b> | $5,000 → <b>${profit(5000):+.2f}</b>",
        f"   $10,000 → <b>${profit(10000):+.2f}</b>",
    ]

    # Пошаговый план
    lines += [
        "",
        "📋 <b>ПОШАГОВЫЙ ПЛАН:</b>",
        f"1️⃣ Купи {_esc(o.base)} на <b>споте {_esc(o.spot_exchange)}</b> по ~<code>{_fmt_price(o.spot_ask)}</code> USDT",
        f"   └─ Тип ордера: LIMIT или MARKET, объём ≥ {_fmt_usd(o.spot_notional_usd)}",
        f"2️⃣ Одновременно открой <b>SHORT</b> на перпетуале <b>{_esc(o.futures_exchange)}</b> по ~<code>{_fmt_price(o.futures_bid)}</code>",
        f"   └─ Плечо: 1x-3x изолированно, хедж-мод, объём = спот-объёму",
        f"3️⃣ Держи хедж: цена {o.base} может идти куда угодно — P&L от цены ≈ 0",
        f"4️⃣ Закрой при схождении: продай спот + выкупи фьючерс, забери +{o.net_spread_percent:.2f}%",
        f"   └─ Или держи для funding: если funding положительный, шорт получает выплаты каждые 8ч",
    ]

    # Альтернативы
    if alternatives:
        alt_lines = "\n".join(
            f"   • {_esc(a.spot_exchange)} → {_esc(a.futures_exchange)}: "
            f"+{a.net_spread_percent:.2f}% ({_fmt_price(a.spot_ask)} → {_fmt_price(a.futures_bid)})"
            for a in alternatives
        )
        lines += ["", "🔀 <b>Топ-альтернативы:</b>", alt_lines]

    # Мета
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🕒 {_fmt_utc(o.created_at)}",
        f"⏳ Повторный сигнал по {_esc(o.base)} не раньше {_fmt_utc(cooldown_until)}",
        "",
        "⚠️ <b>Риски:</b> проверь ликвидность, funding rate, комиссии вывода, проскальзывание.",
        "Бот не торгует — только находит возможности. Решение за тобой.",
        "",
        "<i>💡 Подсказки: /price BTC — цены по всем биржам, /calc BTC 1000 — профит на твой депозит, "
        "/guide — как торговать, /top — топ спредов.</i>",
    ]
    return "\n".join(lines)


def format_startup_message(
    settings: Settings,
    bases: list[str],
    *,
    dead_labels: Optional[list[str]] = None,
) -> str:
    mode = "WebSocket + REST-fallback" if settings.use_websocket else "REST-опрос"
    if settings.symbols:
        source = ", ".join(settings.symbols)
    elif settings.top_symbols_limit > 0:
        source = f"топ-{settings.top_symbols_limit} по объёму торгов"
    else:
        source = "все поддерживаемые монеты"
    lines = [
        "✅ <b>Сканер арбитражных связок запущен</b> — профессиональный режим",
        "",
        f"🏦 Биржи: {_esc(', '.join(e.upper() for e in settings.exchanges))}",
        f"📡 Режим сбора: {_esc(mode)} — реальные цены из стаканов",
        f"🎯 Пар: {len(bases)} ({_esc(source)})",
        f"🚦 Порог сигнала: чистый спред ≥ {settings.min_spread_percent:.2f}%",
        f"💳 Комиссии: {settings.spot_taker_fee_percent:.2f}% + "
        f"{settings.futures_taker_fee_percent:.2f}% = {settings.total_fee_percent:.2f}%",
        f"⏳ Кулдаун на пару: {settings.cooldown_minutes:.0f} мин",
    ]
    if dead_labels:
        lines += ["", f"⚠️ Недоступны: {_esc(', '.join(dead_labels))} — бот работает с остальными"]
    lines += [
        "",
        "📋 <b>Что я делаю:</b>",
        "• Сканирую ВСЕ монеты: спот на одной бирже → фьючерс на другой",
        "• Считаю чистый спред: (FutBid - SpotAsk)/SpotAsk*100 - комиссии",
        "• Присылаю сигнал только если спред ≥ порога",
        "",
        "💡 <b>Как пользоваться:</b>",
        "/top — топ спредов с ценами покупки/продажи",
        "/price BTC — все цены BTC по биржам + лучшая связка",
        "/calc BTC 1000 — сколько заработаешь с $1000",
        "/guide — полный гайд по арбитражу",
        "/strategy — стратегии и риски",
        "/status — статус бирж и аптайм",
        "",
        "<i>Ищу связки: покупка спота — продажа фьючерса (хедж). Без API-ключей, только публичные данные.</i>",
    ]
    return "\n".join(lines)


def format_heartbeat_message(scanner: ArbitrageScanner) -> str:
    sides = scanner._live_sides()
    fresh = total = 0
    for side in sides:
        f, t = side.fresh_quotes_count()
        fresh += f
        total += t
    best = "нет данных" if scanner._last_best is None else (
        f"{scanner._last_best[0]:+.2f}% ({_esc(scanner._last_best[1])}, {_esc(scanner._last_best[2])})"
    )
    now = time.time()
    active_cooldowns = sum(1 for until in scanner._cooldown_until.values() if until > now)
    return "\n".join([
        "💚 <b>Сканер работает — всё в порядке</b>",
        "",
        f"⏱ Аптайм: {_fmt_duration(now - scanner._started_at)}",
        f"📊 Сканов: {scanner.stats['scans']} · Сигналов: {scanner.stats['signals_sent']} "
        f"(подавлено кулдауном: {scanner.stats['signals_suppressed_cooldown']})",
        f"📡 Свежих стаканов: {fresh}/{total} | Проверено комбинаций: {scanner.stats['combinations_checked']}",
        f"🔥 Лучший чистый спред сейчас: {best}",
        f"⏳ Пар в кулдауне: {active_cooldowns} | Пар отслеживается: {len(scanner.bases)}",
        "",
        "<i>Используй /top чтобы увидеть актуальные спреды, /status — детали по биржам.</i>",
    ])


def format_status_message(scanner: ArbitrageScanner) -> str:
    """Подробный статус по команде /status: heartbeat + состояние бирж."""
    header = format_heartbeat_message(scanner)
    side_lines = ["", "🏦 <b>Биржи — детально:</b>"]
    for side in scanner.spot_sides + scanner.futures_sides:
        if side.alive:
            fresh, total = side.fresh_quotes_count()
            mode = "⚡ WS" if side.mode == "ws" else "🔄 REST"
            emoji = "✅"
            side_lines.append(
                f"   {emoji} {_esc(side.label)}: {len(side.symbols)} пар, {mode}, "
                f"свежих {fresh}/{total}, ошибок {side.error_count}"
            )
        else:
            side_lines.append(f"   ❌ {_esc(side.label)}: недоступна")
    side_lines += [
        "",
        f"⚙️ Конфиг: порог {scanner.settings.min_spread_percent:.2f}%, "
        f"комиссии {scanner.settings.total_fee_percent:.2f}%, "
        f"кулдаун {scanner.settings.cooldown_minutes:.0f}м, "
        f"режим {'WS+REST' if scanner.settings.use_websocket else 'REST'}",
    ]
    return "\n".join([header] + side_lines)


def format_help_message(settings: Settings) -> str:
    return "\n".join([
        "🤖 <b>Сканер арбитражных связок — профессиональный бот</b>",
        "",
        "Слежу за стаканами 5 бирж (MEXC, Bybit, Gate, OKX, Binance) в реальном времени "
        f"и присылаю сигнал, как только чистый спред ≥ {settings.min_spread_percent:.2f}% "
        f"(после комиссий {settings.total_fee_percent:.2f}%).",
        "",
        "<b>📊 Команды:</b>",
        "/top [N] — топ-N чистых спредов с ценами (по умолчанию 10)",
        "/price BTC — все цены BTC по биржам + лучшая связка + план действий",
        "/calc BTC 1000 — калькулятор профита: сколько заработаешь с $1000 на BTC",
        "/status — статус бота, бирж, аптайм, лучший спред",
        "/exchanges — детальный статус каждой биржи (WS/REST, ошибки)",
        "/guide — полный гайд: как торговать арбитраж, пошагово",
        "/strategy — стратегии, риски, funding, комиссии",
        "/help — эта справка",
        "",
        "<b>💡 Как читать сигнал:</b>",
        "📥 Купить спот — где и по какой цене покупать (ask — цена покупки)",
        "📤 Продать фьючерс — где и по какой цене шортить (bid — цена продажи)",
        "💎 Чистый спред — твой профит % после комиссий",
        "💰 Профит — примеры для $100 / $1000 / $5000",
        "📋 План — что нажать на бирже, шаг за шагом",
        "",
        "<b>🔁 Схема хеджа:</b>",
        "LONG спот + SHORT perp = нет риска направления. "
        "Если BTC растёт — спот в плюсе, фьючерс в минусе, суммарно 0 + спред. "
        "Если падает — наоборот. Забираешь только разницу цен.",
        "",
        "Или просто нажми кнопку ниже 👇 — всё покажу с реальными ценами!",
    ])


def format_guide_message() -> str:
    return "\n".join([
        "📚 <b>ПОЛНЫЙ ГАЙД ПО МЕЖБИРЖЕВОМУ АРБИТРАЖУ SPOT → FUTURES</b>",
        "",
        "<b>1. Что такое арбитраж?</b>",
        "Покупаешь дёшево на одной бирже, продаёшь дорого на другой. "
        "Разница — твой профит. В нашем случае: спот дешевле фьючерса.",
        "",
        "<b>2. Почему это работает?</b>",
        "• Разные биржи — разная ликвидность, разные участники",
        "• Фьючерсы часто дороже спота (contango) из-за funding и ожиданий",
        "• Цены сходятся со временем — забираешь спред",
        "",
        "<b>3. Пошагово — как исполнить сигнал бота:</b>",
        "Пример сигнала: BTC +2.85% — Buy MEXC @ 67234 → Sell Bybit @ 69150",
        "",
        "Шаг 1️⃣: <b>Купи спот</b>",
        "• Иди на MEXC → Спот → BTC/USDT",
        "• Купи на $1000 по ~67234 (LIMIT или MARKET)",
        "• Проверь: комиссия ~0.1% = $1",
        "",
        "Шаг 2️⃣: <b>Открой SHORT на фьючерсе</b>",
        "• Иди на Bybit → Деривативы → BTCUSDT perpetual",
        "• Открой SHORT на $1000 по ~69150, плечо 1x-3x изолированно",
        "• Комиссия ~0.05% = $0.5",
        "",
        "Шаг 3️⃣: <b>Ты в хедже</b>",
        "• LONG спот + SHORT perp = delta-neutral",
        "• Цена BTC не важна — P&L от цены ≈ 0",
        "• Твой профит зафиксирован: +2.85% = $28.5 с $1000",
        "",
        "Шаг 4️⃣: <b>Закрой</b>",
        "• Когда спред сошёлся: продай спот + выкупи фьючерс",
        "• Или держи: если funding положительный, шорт получает выплаты каждые 8ч",
        "",
        "<b>4. Где смотреть цены?</b>",
        "/price BTC — покажет все цены BTC по биржам и лучшую связку",
        "/calc BTC 1000 — посчитает профит для твоего депозита",
        "/top — топ спредов прямо сейчас",
        "",
        "<b>5. Риски:</b>",
        "• Проскальзывание — ставь лимитки, проверяй глубину стакана",
        "• Funding rate — может быть отрицательным, тогда шорт платит",
        "• Комиссии вывода — если нужно перегонять монеты",
        "• Ликвидация — не используй высокое плечо, держи запас маржи",
        "• Биржа недоступна — бот сам переключится, но проверь /status",
        "",
        "<b>6. Советы профи:</b>",
        "• Начинай с $100-500, проверь механику",
        "• Используй лимитные ордера для лучшей цены",
        "• Держи USDT на обеих биржах заранее",
        "• Следи за funding на Coinglass / биржах",
        "• Не гонись за >5% спредом — часто низколиквид",
        "",
        "💡 <i>Бот не торгует за тебя — он находит возможности. Решение и исполнение — твои.</i>",
        "📊 Начни с /top и /price BTC!",
    ])


def format_strategy_message(settings: Settings) -> str:
    return "\n".join([
        "🧠 <b>СТРАТЕГИИ И МАТЕМАТИКА АРБИТРАЖА</b>",
        "",
        "<b>Формула:</b>",
        "Gross = (Futures_Bid - Spot_Ask) / Spot_Ask * 100%",
        f"Net = Gross - Fees ({settings.spot_taker_fee_percent:.2f}% + {settings.futures_taker_fee_percent:.2f}% = {settings.total_fee_percent:.2f}%)",
        "Сигнал если Net ≥ " + f"{settings.min_spread_percent:.2f}%",
        "",
        "<b>Пример:</b>",
        "Spot Ask MEXC = 100.00, Futures Bid Bybit = 103.00",
        "Gross = (103-100)/100*100 = 3.00%",
        f"Net = 3.00% - {settings.total_fee_percent:.2f}% = {3.0 - settings.total_fee_percent:.2f}%",
        f"Депозит $1000 → профит ${1000 * (3.0 - settings.total_fee_percent) / 100:.2f}",
        "",
        "<b>Типы арбитража:</b>",
        "1️⃣ <b>Spot → Futures (наш основной):</b>",
        "   Buy spot, Short perp. Работает когда фьючерс дороже спота (contango).",
        "   Плюс: получаешь funding если он положительный (шорт получает).",
        "",
        "2️⃣ <b>Futures → Spot (обратный):</b>",
        "   Sell spot (нужен спот в наличии или маржа) + Long perp.",
        "   Работает когда спот дороже фьючерса (backwardation).",
        "   Сейчас бот ловит только первый тип, но в /price видно оба направления.",
        "",
        "3️⃣ <b>Spot → Spot (межбиржевой):</b>",
        "   Buy spot на бирже A, Sell spot на бирже B. Нужен перевод по сети.",
        "   Минус: время перевода, комиссии сети, риск цены. Бот пока не сигналит, "
        "но /price показывает разницу спотов.",
        "",
        "4️⃣ <b>Futures → Futures:</b>",
        "   Long perp на одной, Short perp на другой. Без спота.",
        "   Плюс: не нужен спот, только маржа. Минус: funding с двух сторон.",
        "",
        "<b>Funding Rate:</b>",
        "Каждые 8ч лонги платят шортам (или наоборот). Если funding +0.01% каждые 8ч, "
        "шорт получает 0.03% в день = ~10% годовых сверху спреда.",
        "Где смотреть: биржа → BTCUSDT → Funding Rate, или Coinglass.",
        "",
        "<b>Риски и как их снижать:</b>",
        "• <b>Проскальзывание:</b> смотри глубину в сигнале, используй лимитки",
        "• <b>Funding:</b> проверяй, если отрицательный — шорт платит",
        "• <b>Ликвидация:</b> плечо 1x-3x, изолированная маржа, стоп на споте",
        "• <b>Комиссии вывода:</b> держи USDT на обеих биржах, не гоняй монеты",
        "• <b>Биржа легла:</b> бот покажет в /status, торгуй на живых",
        "",
        "<b>Вдохновлено лучшими ботами с GitHub:</b>",
        "• OKEx V5 Futures-Spot Arbitrage (155★) — анализ funding и волатильности",
        "• Cross-Exchange AI Arbitrage — сканер спредов CEX/DEX",
        "• Mammuth Bitcoin Arbitrage — мониторинг и Web UI",
        "• Наш бот берёт лучшее: реальные стаканы, WS+REST, понятные сигналы, калькулятор",
        "",
        f"⚙️ Текущий порог: {settings.min_spread_percent:.2f}%, комиссии {settings.total_fee_percent:.2f}%, "
        f"кулдаун {settings.cooldown_minutes:.0f}м",
        "",
        "<i>Используй /guide для пошагового плана, /calc для расчёта профита.</i>",
    ])
