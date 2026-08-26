"""
scanner.py — ядро сканера арбитражных связок «Spot ↔ Futures» (v2.0).

v2.0 — профи-режим «только по запросу» (как в ArbitrageScanner):
  * бот по умолчанию НИЧЕГО не присылает сам — никакого спама;
  * сигналы выдаёт по командам: /top, /signal, /coin, /funding, /coins;
  * честные таблицы в столбиках: цена на каждой бирже, где покупать,
    где продавать (шортить), чистый спред, ликвидность, funding;
  * оба направления: S>F (купить спот / шорт перп) и F>S (лонг перп /
    продать спот) — и contango, и backwardation.

Архитектура
-----------
      ┌──────────────────────────────────────────────────────────┐
      │                   ArbitrageScanner                       │
      │  инициализация рынков → подбор пар → сборщики цен        │
      └──────────────────────────┬───────────────────────────────┘
   ┌─────────────────────────────┼─────────────────────────────┐
   │ ExchangeSide (MEXC spot)    │ ExchangeSide (Bybit futures) │ × N сторон
   │  WebSocket: watch_order_book│  WebSocket / REST fallback   │
   └─────────────────────────────┼─────────────────────────────┘
                 кэш bid/ask с метками времени (свежесть ≤ BOOK_MAX_AGE)
   ┌─────────────────────────────▼─────────────────────────────┐
   │ ФОН (каждые SCAN_INTERVAL_SECONDS) — только расчёт:       │
   │   * лучший спред для /status;                             │
   │   * on_demand: события в журнал /signals (без отправки);  │
   │   * auto (опц.): push-сигналы с кулдауном и лимитом.      │
   └─────────────────────────────┬─────────────────────────────┘
                 по запросу: полный прогон матрицы цен (обе стороны
                 спот × фьючерс) → HTML-ответ с таблицами и планом
                                   │
                                   ▼
                     TelegramNotifier.send_html_to_chat()

Ключевые решения:
* Только ПУБЛИЧНЫЕ эндпоинты и WebSocket — никаких API-ключей.
* ccxt.pro (входит в пакет ccxt): watch_order_book + fetch_order_book,
  автоматическая деградация WS → REST при сбоях.
* enableRateLimit=True + троттлинг — защита от банов IP.
* Funding-рейты и объёмы 24ч — по запросу, с кэшем (не нагружает API).
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, replace
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
# Направления арбитража
# ---------------------------------------------------------------------------

DIR_SPOT_TO_FUT = "S>F"   # купить спот (ask) + шорт перпетуала (bid) — контанго
DIR_FUT_TO_SPOT = "F>S"   # лонг перпетуала (ask) + продать спот (bid) — бэквардация

DIRECTION_LABELS: dict[str, str] = {
    DIR_SPOT_TO_FUT: "спот → фьючерс (купить спот / шорт перп)",
    DIR_FUT_TO_SPOT: "фьючерс → спот (лонг перп / продать спот)",
}


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
    """
    Найденная арбитражная связка.

    direction:
      S>F — buy_spot + short_perp  (покупаем спот по ask, шортим перп по bid)
      F>S — long_perp + sell_spot  (лонжим перп по ask, продаём спот по bid)
    """

    base: str
    spot_exchange: str
    futures_exchange: str
    spot_symbol: str
    futures_symbol: str
    direction: str = DIR_SPOT_TO_FUT
    spot_ask: float = 0.0
    spot_bid: float = 0.0
    futures_bid: float = 0.0
    futures_ask: float = 0.0
    gross_spread_percent: float = 0.0
    net_spread_percent: float = 0.0
    spot_notional_usd: float = 0.0
    futures_notional_usd: float = 0.0
    same_exchange: bool = False
    created_at: float = 0.0
    data_age_seconds: float = 0.0
    funding_rate_percent: Optional[float] = None  # % за 8ч на фьючерсной бирже

    # --- удобные свойства ----------------------------------------------------
    @property
    def is_spot_first(self) -> bool:
        return self.direction == DIR_SPOT_TO_FUT

    @property
    def buy_exchange(self) -> str:
        return self.spot_exchange if self.is_spot_first else self.futures_exchange

    @property
    def sell_exchange(self) -> str:
        return self.futures_exchange if self.is_spot_first else self.spot_exchange

    @property
    def buy_price(self) -> float:
        return self.spot_ask if self.is_spot_first else self.futures_ask

    @property
    def sell_price(self) -> float:
        return self.futures_bid if self.is_spot_first else self.spot_bid

    @property
    def min_notional_usd(self) -> float:
        return min(self.spot_notional_usd, self.futures_notional_usd)


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
        допустимый возраст автоматически расширяется (2 круга + запас).
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
    """Оркестратор: инициализация бирж, фоновый расчёт, ответы по запросу."""

    #: попыток подключиться к бирже на старте, прежде чем исключить её
    MARKET_LOAD_ATTEMPTS = 3
    #: сколько событий (спред ≥ порога) держать в журнале /signals
    EVENT_LOG_LIMIT = 100
    #: строк на страницу списка монет /coins
    COINS_PAGE_SIZE = 20

    def __init__(self, settings: Settings, notifier: Any) -> None:
        self.settings = settings
        self.notifier = notifier
        self.spot_sides: list[ExchangeSide] = []
        self.futures_sides: list[ExchangeSide] = []
        self.bases: list[str] = []
        self._cooldown_until: dict[str, float] = {}        # auto-режим: антидубль push
        self._last_best: Optional[tuple[float, str, str]] = None  # (net%, base, маршрут)
        self._listener_task: Optional[asyncio.Task] = None
        # Журнал событий on_demand-режима: связки, где спред был ≥ порога.
        self._events: deque[Opportunity] = deque(maxlen=self.EVENT_LOG_LIMIT)
        self._event_cooldown_until: dict[str, float] = {}
        # Кэш funding-рейтов: (exchange_id, base) → (rate% or None, ts)
        self._funding_cache: dict[tuple[str, str], tuple[Optional[float], float]] = {}
        # Кэш объёмов 24ч: (ts, exchange_name, [(base, volume), ...])
        self._volume_cache: Optional[tuple[float, str, list[tuple[str, float]]]] = None
        self.stats: dict[str, int] = {
            "scans": 0,
            "signals_sent": 0,
            "signals_suppressed_cooldown": 0,
            "combinations_checked": 0,
            "events_recorded": 0,
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
            "Сканер запущен: %d живых сторон, %d пар (%s), режим %s",
            len(self._live_sides()), len(self.bases),
            ", ".join(self.bases[:10]) + ("…" if len(self.bases) > 10 else ""),
            self.settings.signal_mode,
        )
        if dead:
            log.warning("Недоступные стороны (пропущены): %s", ", ".join(dead))
        if self.settings.startup_message:
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
            _, ranked = await self._get_volume_ranking()
            ranked_bases = [b for b, _ in ranked if b in set(supported)]
            if ranked_bases:
                return ranked_bases[: s.top_symbols_limit]
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
            if s.signal_mode == "auto":
                await self._process_signals(pairs)
            else:
                # on_demand: ничего не отправляем — только журнал /signals.
                self._record_events(pairs)

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
    def _collect_opportunities(
        self,
        threshold: float,
        direction: str = DIR_SPOT_TO_FUT,
    ) -> dict[str, list[Opportunity]]:
        """
        Считает чистые спреды для всех комбинаций (спот × фьючерс × пара).

        Возвращает {пара: [связки, отсортированные по чистому спреду, desc]}.
        Порог threshold задаёт минимальный чистый спред (float("-inf") = все).
        direction: DIR_SPOT_TO_FUT (S>F) или DIR_FUT_TO_SPOT (F>S).
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
                same_exchange = spot_side.exchange_id == fut_side.exchange_id
                if same_exchange and not self.settings.allow_same_exchange:
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

                    if direction == DIR_SPOT_TO_FUT:
                        if spot_quote.ask <= 0.0 or fut_quote.bid <= 0.0:
                            continue
                        gross = (fut_quote.bid - spot_quote.ask) / spot_quote.ask * 100.0
                        notional = min(spot_quote.ask_notional_usd, fut_quote.bid_notional_usd)
                    else:
                        if spot_quote.bid <= 0.0 or fut_quote.ask <= 0.0:
                            continue
                        gross = (spot_quote.bid - fut_quote.ask) / fut_quote.ask * 100.0
                        notional = min(spot_quote.bid_notional_usd, fut_quote.ask_notional_usd)

                    net = gross - fee_percent
                    self.stats["combinations_checked"] += 1
                    if direction == DIR_SPOT_TO_FUT:
                        self._track_best(net, base, spot_side.display_name, fut_side.display_name)

                    if net < threshold:
                        continue
                    if self.settings.min_notional_usd > 0 and notional < self.settings.min_notional_usd:
                        continue

                    by_base.setdefault(base, []).append(Opportunity(
                        base=base,
                        spot_exchange=spot_side.display_name,
                        futures_exchange=fut_side.display_name,
                        spot_symbol=spot_symbol,
                        futures_symbol=fut_symbol,
                        direction=direction,
                        spot_ask=spot_quote.ask,
                        spot_bid=spot_quote.bid,
                        futures_bid=fut_quote.bid,
                        futures_ask=fut_quote.ask,
                        gross_spread_percent=gross,
                        net_spread_percent=net,
                        spot_notional_usd=(
                            spot_quote.ask_notional_usd if direction == DIR_SPOT_TO_FUT
                            else spot_quote.bid_notional_usd
                        ),
                        futures_notional_usd=(
                            fut_quote.bid_notional_usd if direction == DIR_SPOT_TO_FUT
                            else fut_quote.ask_notional_usd
                        ),
                        same_exchange=same_exchange,
                        created_at=now,
                        data_age_seconds=now - min(spot_quote.timestamp, fut_quote.timestamp),
                    ))

        for opportunities in by_base.values():
            opportunities.sort(key=lambda o: o.net_spread_percent, reverse=True)
        return by_base

    def _evaluate(self) -> list[tuple[Opportunity, list[Opportunity]]]:
        """
        Связки S>F, подходящие под сигнал/журнал: лучшая связка на пару +
        альтернативы, отсортировано по убыванию чистого спреда.
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

    # ------------------------------------------------------------------ сигнал: on_demand (журнал)
    def _record_events(self, pairs: list[tuple[Opportunity, list[Opportunity]]]) -> None:
        """
        on_demand-режим: фиксируем связки с чистым спредом ≥ порога в журнал
        (видно в /signals). Ничего в Telegram НЕ отправляется.
        """
        if not pairs:
            return
        now = time.time()
        for best, _ in pairs:
            if now < self._event_cooldown_until.get(best.base, 0.0):
                continue
            self._events.append(best)
            self._event_cooldown_until[best.base] = now + self.settings.cooldown_seconds
            self.stats["events_recorded"] += 1
            log.info(
                "СОБЫТИЕ %s/USDT: чистый спред +%.2f%% (%s спот → %s фьючерс)",
                best.base, best.net_spread_percent,
                best.spot_exchange, best.futures_exchange,
            )

    # ------------------------------------------------------------------ сигнал: auto (push)
    async def _process_signals(self, pairs: list[tuple[Opportunity, list[Opportunity]]]) -> None:
        """auto-режим: отправляет сигналы с учётом кулдауна и лимита на цикл."""
        if self.settings.signal_mode != "auto":
            return  # on_demand: push запрещён — сигналы только по запросу
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
            "сигналов %d (подавлено кулдауном %d), событий %d, лучший чистый спред: %s",
            len(sides), fresh, total, self.stats["scans"],
            self.stats["signals_sent"], self.stats["signals_suppressed_cooldown"],
            self.stats["events_recorded"], best,
        )

    # ------------------------------------------------------------------ аналитика по запросу
    async def _get_volume_ranking(self) -> tuple[str, list[tuple[str, float]]]:
        """
        Топ всех отслеживаемых баз по 24ч-объёму торгов (quote volume, USDT)
        с одной из живых спот-бирж. Результат кэшируется на
        VOLUME_CACHE_MINUTES. Возвращает (название биржи, [(base, volume)]).
        """
        now = time.time()
        if (
            self._volume_cache is not None
            and now - self._volume_cache[0] < self.settings.volume_cache_minutes * 60.0
        ):
            return self._volume_cache[1], self._volume_cache[2]

        for side in self.spot_sides:
            if not side.alive:
                continue
            # markets должны быть загружены (в тестовых заглушках — нет)
            if not getattr(side.exchange, "markets", None):
                continue
            try:
                tickers = await asyncio.wait_for(side.exchange.fetch_tickers(), timeout=60.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Объёмы 24ч: %s не отдала тикеры (%s) — пробую следующую",
                            side.label, exc)
                continue

            scored: list[tuple[str, float]] = []
            bases_set = set(self.bases)
            for symbol, ticker in (tickers or {}).items():
                market = (side.exchange.markets or {}).get(symbol)
                if not market or market.get("type") != "spot":
                    continue
                if market.get("quote") != "USDT":
                    continue
                base = market.get("base") or ""
                if base not in bases_set:
                    continue
                try:
                    volume = float(ticker.get("quoteVolume") or 0.0)
                except (TypeError, ValueError):
                    continue
                scored.append((base, volume))

            scored.sort(key=lambda item: item[1], reverse=True)
            if scored:
                self._volume_cache = (now, side.display_name, scored)
                return side.display_name, scored
        return "", []

    async def _get_funding_map(self, base: str) -> dict[str, Optional[float]]:
        """
        Funding-рейты % за 8ч по всем живым фьючерсным биржам с этой базой.
        Возвращает {НазваниеБиржи: rate% or None} — с кэшем
        FUNDING_CACHE_MINUTES. Все сбои гасятся (None), сеть не роняет.
        """
        if not self.settings.funding_enabled:
            return {}
        now = time.time()
        ttl = self.settings.funding_cache_minutes * 60.0
        sides = [s for s in self.futures_sides if s.alive and base in s.symbol_by_base]
        result: dict[str, Optional[float]] = {}
        to_fetch: list[ExchangeSide] = []

        for side in sides:
            cached = self._funding_cache.get((side.exchange_id, base))
            if cached is not None and now - cached[1] < ttl:
                result[side.display_name] = cached[0]
            else:
                to_fetch.append(side)

        async def fetch_one(side: ExchangeSide) -> Optional[float]:
            if not getattr(side.exchange, "markets", None):
                return None  # рынки не загружены (offline/тест) — не ходим в сеть
            try:
                value = await asyncio.wait_for(
                    side.exchange.fetch_funding_rate(side.symbol_by_base[base]),
                    timeout=4.0,
                )
                rate = float((value or {}).get("fundingRate"))  # доля, не %
                return rate * 100.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — funding не критичен
                log.debug("%s: funding %s: %s", side.label, base, exc)
                return None

        fetched = await asyncio.gather(*(fetch_one(s) for s in to_fetch))
        for side, rate in zip(to_fetch, fetched):
            self._funding_cache[(side.exchange_id, base)] = (rate, now)
            result[side.display_name] = rate
        return result

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
            "scan": self._cmd_top,
            "signal": self._cmd_signal,
            "price": self._cmd_price,
            "coin": self._cmd_price,
            "coins": self._cmd_coins,
            "funding": self._cmd_funding,
            "signals": self._cmd_signals,
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
        """
        /top [N] — топ-N чистых спредов (таблица), направление S>F.
        /top fs [N] — обратное направление F>S (лонг перп → продать спот).
        """
        tokens = args.split()
        direction = DIR_SPOT_TO_FUT
        limit = 10
        for token in tokens:
            low = token.lower()
            if low in ("fs", "f>s", "f→s", "reverse", "обратный"):
                direction = DIR_FUT_TO_SPOT
            elif token.isdigit():
                limit = max(1, min(20, int(token)))
        return await self._build_top_message(limit, direction)

    async def _cmd_signal(self, chat_id: str, args: str) -> str:
        """
        /signal [COIN] — лучшая связка ПРЯМО СЕЙЧАС (детальный разбор):
        без аргумента — лучшая из всех монет (оба направления),
        с аргументом — лучшая для указанной монеты.
        """
        base = (args.split()[0] if args.strip() else "").upper() or None
        return await self._build_signal_message(base)

    async def _cmd_price(self, chat_id: str, args: str) -> str:
        base = (args.split()[0] if args.strip() else "BTC").upper()
        return await self._build_price_message(base)

    async def _cmd_coins(self, chat_id: str, args: str) -> str:
        """
        /coins [PAGE] — список сканируемых монет (по объёму 24ч) + лучший
        спред по каждой. Страница по 20 монет.
        """
        tokens = args.split()
        page = 1
        if tokens and tokens[0].isdigit():
            page = max(1, min(10_000, int(tokens[0])))
        return await self._build_coins_message(page)

    async def _cmd_funding(self, chat_id: str, args: str) -> str:
        base = (args.split()[0] if args.strip() else "BTC").upper()
        return await self._build_funding_message(base)

    async def _cmd_signals(self, chat_id: str, args: str) -> str:
        return self._build_signals_log_message()

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
    async def _build_top_message(self, limit: int, direction: str) -> str:
        """Топ чистых спредов — таблица в столбиках (monospace)."""
        now = time.time()
        by_base = self._collect_opportunities(threshold=float("-inf"), direction=direction)
        best_list = [opps[0] for opps in by_base.values()]
        best_list.sort(key=lambda o: o.net_spread_percent, reverse=True)

        if not best_list:
            return (
                "📊 <b>Топ спредов</b>: нет свежих данных со стаканов — подождите пару секунд.\n"
                "Биржи загружаются, WebSocket подключается...\n\n"
                "Проверьте /status — состояние бирж."
            )

        if direction == DIR_SPOT_TO_FUT:
            header_note = "S→F: купить спот (самый низкий ask) + шорт перп (самый высокий bid)"
            buy_col, sell_col = "КУПИТЬ (СПОТ)", "ШОРТ (ПЕРП)"
        else:
            header_note = "F>S: лонг перп (самый низкий ask) + продать спот (самый высокий bid)"
            buy_col, sell_col = "ЛОНГ (ПЕРП)", "ПРОДАТЬ (СПОТ)"

        rows = []
        for position, opp in enumerate(best_list[:limit], start=1):
            rows.append([
                str(position),
                opp.base[:8],
                f"{opp.buy_exchange[:8]} {_fmt_price(opp.buy_price)}",
                f"{opp.sell_exchange[:8]} {_fmt_price(opp.sell_price)}",
                f"{opp.net_spread_percent:+.2f}%",
                _fmt_usd_human(opp.min_notional_usd),
            ])
        table = _mono_table(
            ["#", "МОНТА", buy_col, sell_col, "NET", "ЛИКВ"], rows
        )

        above = sum(1 for o in best_list if o.net_spread_percent >= self.settings.min_spread_percent)
        lines = [
            f"📊 <b>ТОП-{min(limit, len(best_list))} СПРЕДОВ ПРЯМО СЕЙЧАС</b>",
            f"<i>{_fmt_utc_short(now)} · {header_note}</i>",
            "",
            f"<pre>{table}</pre>",
            "",
            f"💳 NET — после комиссий {self.settings.total_fee_percent:.2f}% | 💧 ЛИКВ — глубина на лучшей цене (min по сторонам)",
            f"🚦 Порог: {self.settings.min_spread_percent:.2f}% | над порогом: {above} из {len(best_list)} пар",
        ]
        if above > 0:
            lines.append(f"🔥 = спред ≥ порога (сигнал в /signal / /coin)")
        lines += [
            "",
            "💡 <b>Как читать:</b> левая колонка — где ДЁШЕВО купить, правая — где ДОРОГО продать/шортить.",
            "   /coin BTC — полный разбор монеты (все цены + план). /top fs — обратное направление.",
        ]
        return "\n".join(lines)

    async def _build_price_message(self, base: str) -> str:
        """
        Полный разбор монеты: реальные цены на КАЖДОЙ бирже, где купить,
        где продавать (шортить), лучшая связка, funding, план действий.
        """
        now = time.time()

        spot_quotes: list[tuple[ExchangeSide, BookQuote]] = []
        fut_quotes: list[tuple[ExchangeSide, BookQuote]] = []

        for side in self.spot_sides:
            if not side.alive:
                continue
            symbol = side.symbol_by_base.get(base)
            quote = side.quotes.get(symbol) if symbol else None
            if quote is not None and quote.timestamp >= now - side.effective_book_max_age():
                spot_quotes.append((side, quote))
        for side in self.futures_sides:
            if not side.alive:
                continue
            symbol = side.symbol_by_base.get(base)
            quote = side.quotes.get(symbol) if symbol else None
            if quote is not None and quote.timestamp >= now - side.effective_book_max_age():
                fut_quotes.append((side, quote))

        if not spot_quotes and not fut_quotes:
            return (
                f"💠 <b>{_esc(base)}/USDT — нет свежих данных</b>\n\n"
                f"Возможные причины:\n"
                f"• Пара не торгуется на выбранных биржах\n"
                f"• Биржи ещё загружаются (посмотрите /status)\n"
                f"• Монета вне списка (проверьте /coins)\n\n"
                f"Попробуйте /top — активные пары прямо сейчас."
            )

        funding = await self._get_funding_map(base) if self.settings.funding_enabled else {}
        by_base_s = self._collect_opportunities(threshold=float("-inf"), direction=DIR_SPOT_TO_FUT)
        by_base_f = self._collect_opportunities(threshold=float("-inf"), direction=DIR_FUT_TO_SPOT)
        opps_s = by_base_s.get(base, [])
        opps_f = by_base_f.get(base, [])

        lines = [
            f"💠 <b>{_esc(base)}/USDT — РАЗБОР ПО БИРЖАМ</b>",
            f"<i>{_fmt_utc(now)} · данные из стаканов</i>",
            "",
        ]

        # --- таблица спотов ------------------------------------------------
        if spot_quotes:
            best_ask = min(q.ask for _, q in spot_quotes)
            lines.append("<b>📥 СПОТ USDT</b> — <i>ask = цена покупки (ниже лучше), bid = цена продажи</i>")
            rows = []
            for side, q in sorted(spot_quotes, key=lambda x: x[1].ask):
                mark = " ←" if q.ask == best_ask else ""
                rows.append([
                    side.display_name[:8],
                    _fmt_price(q.ask) + mark,
                    _fmt_price(q.bid),
                    _fmt_usd_human(q.ask_notional_usd),
                    f"{now - q.timestamp:.0f}с",
                ])
            lines.append(
                "<pre>"
                + _mono_table(
                    ["Биржа", "ASK купить", "BID продать", "Глубина ASK", "Возраст"],
                    rows,
                )
                + "</pre>"
            )
            lines.append("")

        # --- таблица фьючерсов ---------------------------------------------
        if fut_quotes:
            best_bid = max(q.bid for _, q in fut_quotes)
            lines.append("<b>📤 ПЕРПЕТУАЛЫ USDT</b> — <i>bid = цена шорта (выше лучше), ask = цена лонга</i>")
            rows = []
            for side, q in sorted(fut_quotes, key=lambda x: x[1].bid, reverse=True):
                mark = " ←" if q.bid == best_bid else ""
                rate = funding.get(side.display_name)
                rows.append([
                    side.display_name[:8],
                    _fmt_price(q.bid) + mark,
                    _fmt_price(q.ask),
                    f"{rate:+.4f}%" if rate is not None else "—",
                    _fmt_usd_human(q.bid_notional_usd),
                ])
            lines.append(
                "<pre>"
                + _mono_table(
                    ["Биржа", "BID шорт", "ASK лонг", "Funding/8ч", "Глубина BID"],
                    rows,
                )
                + "</pre>"
            )
            lines.append("")

        # --- лучшая связка --------------------------------------------------
        candidates: list[Opportunity] = []
        if opps_s:
            candidates.append(opps_s[0])
        if opps_f:
            candidates.append(opps_f[0])

        if candidates:
            main = max(candidates, key=lambda o: o.net_spread_percent)
            other = next((c for c in candidates if c is not main), None)
            lines.extend(self._format_best_combo_block(base, main, funding))

            if other is not None:
                verdict = "✅" if other.net_spread_percent >= 0 else "❌"
                note = f" · нужен инвентарь {base} на спот-бирже" if other.direction == DIR_FUT_TO_SPOT else ""
                lines += [
                    "",
                    f"🔁 <b>Обратное направление {other.direction.replace('>', '→')}</b> "
                    f"({_esc(DIRECTION_LABELS[other.direction])}):",
                    f"   {other.buy_exchange} <code>{_fmt_price(other.buy_price)}</code> → "
                    f"{other.sell_exchange} <code>{_fmt_price(other.sell_price)}</code> = "
                    f"<b>{other.net_spread_percent:+.2f}%</b> {verdict}{note}",
                ]
        else:
            # Связок нет (например, только одна сторона) — считаем «в лоб».
            if spot_quotes and fut_quotes:
                best_spot = min(spot_quotes, key=lambda x: x[1].ask)
                best_fut = max(fut_quotes, key=lambda x: x[1].bid)
                gross = (best_fut[1].bid - best_spot[1].ask) / best_spot[1].ask * 100.0
                net = gross - self.settings.total_fee_percent
                verdict = "✅ над порогом" if net >= self.settings.min_spread_percent else "❌ ниже порога"
                lines += [
                    "━━━━━━━━━━━━━━━━━━━━",
                    "📊 <b>Расчёт по лучшим ценам (S→F):</b>",
                    f"   спот ask: {_esc(best_spot[0].display_name)} <code>{_fmt_price(best_spot[1].ask)}</code>",
                    f"   перп bid: {_esc(best_fut[0].display_name)} <code>{_fmt_price(best_fut[1].bid)}</code>",
                    f"   Gross {gross:+.2f}% − fees {self.settings.total_fee_percent:.2f}% = "
                    f"<b>{net:+.2f}%</b> ({verdict}, порог {self.settings.min_spread_percent:.2f}%)",
                ]

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"💡 /signal {base} — сигнал с планом · /calc {base} 1000 — профит на свой депозит · "
            f"/funding {base} — funding по всем биржам",
        ]
        return "\n".join(lines)

    def _format_best_combo_block(
        self, base: str, opp: Opportunity, funding: dict[str, Optional[float]]
    ) -> list[str]:
        """Блок «лучшая связка»: таблица, профит, план действий."""
        s = self.settings
        rate = opp.funding_rate_percent
        if rate is None:
            rate = funding.get(opp.futures_exchange)

        fire = " ✅ над порогом" if opp.net_spread_percent >= s.min_spread_percent else " ❌ ниже порога"
        combo_rows = [
            ["купить", f"{opp.buy_exchange}  {_fmt_price(opp.buy_price)}"],
            ["продать", f"{opp.sell_exchange}  {_fmt_price(opp.sell_price)}"],
            ["gross", f"{opp.gross_spread_percent:+.2f}%"],
            ["fees", f"-{s.total_fee_percent:.2f}% (спот {s.spot_taker_fee_percent:.2f}% + перп {s.futures_taker_fee_percent:.2f}%)"],
            ["NET", f"{opp.net_spread_percent:+.2f}%{fire}"],
        ]
        if rate is not None:
            holder = "шорт" if opp.is_spot_first else "лонг"
            combo_rows.append(
                ["funding", f"{rate:+.4f}%/8ч на {opp.futures_exchange} ({holder} {'получает' if rate >= 0 else 'платит'})"]
            )
        combo_rows.append(["ликвидн.", f"{_fmt_usd(opp.min_notional_usd)} · данные ≤ {opp.data_age_seconds:.0f}с"])

        block = [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🏆 <b>ЛУЧШАЯ СВЯЗКА {opp.direction.replace('>', '→')} · {_esc(base)}</b> — {_esc(DIRECTION_LABELS[opp.direction])}",
            "<pre>" + _mono_table(["Параметр", "Значение"], combo_rows) + "</pre>",
            "",
            f"💰 <b>Профит:</b> $100 → <b>${100 * opp.net_spread_percent / 100:+.2f}</b> · "
            f"$1,000 → <b>${1000 * opp.net_spread_percent / 100:+.2f}</b> · "
            f"$5,000 → <b>${5000 * opp.net_spread_percent / 100:+.2f}</b>",
            "",
            "<b>📋 ПЛАН ДЕЙСТВИЙ:</b>",
        ]

        if opp.is_spot_first:
            plan = [
                f"1️⃣ Купи <b>{base}</b> на споте <b>{_esc(opp.buy_exchange)}</b> по ~<code>{_fmt_price(opp.buy_price)}</code> USDT (лимитка/маркет)",
                f"2️⃣ Одновременно открой <b>SHORT</b> перпетуала <b>{_esc(opp.sell_exchange)}</b> по ~<code>{_fmt_price(opp.sell_price)}</code> (1x–3x, изолированная маржа)",
                f"3️⃣ Хедж зафиксирован: LONG спот + SHORT перп → P&L от цены ≈ 0, спред {opp.net_spread_percent:+.2f}% заперт",
                f"4️⃣ Выход: продай спот + выкупи шорт, когда спред сошёлся. Или держи — шорт получает funding (если +)",
            ]
        else:
            plan = [
                f"1️⃣ Купи <b>перпетуал {base}</b> (LONG) на <b>{_esc(opp.buy_exchange)}</b> по ~<code>{_fmt_price(opp.buy_price)}</code> (1x–3x, изолированная маржа)",
                f"2️⃣ Одновременно <b>продай {base} на споте</b> <b>{_esc(opp.sell_exchange)}</b> по ~<code>{_fmt_price(opp.sell_price)}</code>",
                "   └─ нужен инвентарь монеты на спот-бирже (купить заранее / перенести)",
                f"3️⃣ Хедж зафиксирован: LONG перп + SHORT спот → P&L от цены ≈ 0, спред {opp.net_spread_percent:+.2f}% заперт",
                f"4️⃣ Выход: выкупи лонг + купи спот обратно, когда спред сошёлся",
            ]
        block += plan
        return block

    async def _build_signal_message(self, base: Optional[str]) -> str:
        """
        Сигнал по запросу: лучшая связка прямо сейчас (детальный разбор).
        base=None — лучшая из всех монет; иначе — для указанной монеты.
        """
        by_s = self._collect_opportunities(threshold=float("-inf"), direction=DIR_SPOT_TO_FUT)
        by_f = self._collect_opportunities(threshold=float("-inf"), direction=DIR_FUT_TO_SPOT)

        if base:
            if base not in by_s and base not in by_f:
                return (
                    f"🎯 <b>Сигнал · {_esc(base)}</b>: нет свежих данных по монете.\n\n"
                    f"Проверьте /coins — список сканируемых, /status — состояние бирж."
                )
            pool = by_s.get(base, []) + by_f.get(base, [])
        else:
            pool = [lst[0] for lst in by_s.values() if lst] + [lst[0] for lst in by_f.values() if lst]

        if not pool:
            return "🎯 <b>Сигнал</b>: сейчас нет свежих связок — биржи ещё подключаются или стаканов нет."

        pool.sort(key=lambda o: o.net_spread_percent, reverse=True)
        best = pool[0]
        alternatives = pool[1:4]

        # Funding для фьючерсной биржи связки (best-effort, кэшируется).
        if self.settings.funding_enabled:
            funding_map = await self._get_funding_map(best.base)
            best = replace(best, funding_rate_percent=funding_map.get(best.futures_exchange))

        return format_signal_message(best, alternatives, self.settings, cooldown_until=0.0)

    async def _build_coins_message(self, page: int) -> str:
        """Список монет: топ по объёму 24ч + лучший спред по каждой."""
        now = time.time()
        ref_name, ranked = await self._get_volume_ranking()
        volume_by_base = dict(ranked)

        # Порядок: сначала по объёму (если есть), остальное — алфавитом.
        ranked_bases = [b for b, _ in ranked]
        rest = sorted(set(self.bases) - set(ranked_bases))
        ordered = ranked_bases + rest
        total_pages = max(1, (len(ordered) + self.COINS_PAGE_SIZE - 1) // self.COINS_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        chunk = ordered[(page - 1) * self.COINS_PAGE_SIZE : page * self.COINS_PAGE_SIZE]

        by_base_best = self._collect_opportunities(threshold=float("-inf"), direction=DIR_SPOT_TO_FUT)

        rows = []
        for position, base in enumerate(chunk, start=1):
            global_index = (page - 1) * self.COINS_PAGE_SIZE + position
            spot_count = sum(1 for s in self.spot_sides if s.alive and base in s.symbol_by_base)
            fut_count = sum(1 for s in self.futures_sides if s.alive and base in s.symbol_by_base)
            volume = volume_by_base.get(base)
            best_list = by_base_best.get(base, [])
            net = f"{best_list[0].net_spread_percent:+.2f}%" if best_list else "—"
            rows.append([
                str(global_index),
                base[:8],
                _fmt_usd_human(volume) if volume is not None else "—",
                str(spot_count),
                str(fut_count),
                net,
            ])

        volume_note = (
            f"объёмы 24ч: {ref_name}" if ref_name
            else "объёмы 24ч: нет данных"
        )
        lines = [
            f"🪙 <b>МОНЕТЫ · страница {page}/{total_pages}</b>",
            f"<i>Сканируется {len(self.bases)} пар (USDT-спот + USDT-перп на ≥1 бирже) · {volume_note}</i>",
            "",
            "<pre>" + _mono_table(
                ["#", "МОНТА", "ОБЪЁМ 24Ч", "СПОТ", "ПЕРП", "СПРЕД"], rows
            ) + "</pre>",
            "",
            "СПОТ/ПЕРП — на скольких живых биржах есть рынок · СПРЕД — лучший чистый (S→F)",
        ]
        if page < total_pages:
            lines.append(f"➡️ /coins {page + 1} — следующая страница")
        if page > 1:
            lines.append(f"⬅️ /coins {page - 1} — назад")
        lines.append(f"💡 /coin BTC — разбор монеты · /top — топ спредов")
        return "\n".join(lines)

    async def _build_funding_message(self, base: str) -> str:
        """Funding-рейты по всем фьючерсным биржам для монеты."""
        if not self.settings.funding_enabled:
            return "💰 <b>Funding</b> — отключён (FUNDING_ENABLED=false в настройках)."
        funding_map = await self._get_funding_map(base)
        available = {name: rate for name, rate in funding_map.items() if rate is not None}

        if not available:
            return (
                f"💰 <b>Funding · {_esc(base)}</b>: нет данных.\n\n"
                f"Биржи не предоставили funding-рейт (или база не торгуется на фьючерсах).\n"
                f"Проверьте /coin {base} — есть ли вообще монета в сканере."
            )

        rows = []
        for name in sorted(available, key=lambda n: available[n], reverse=True):
            rate = available[name]
            apr = rate * 3 * 365.0
            payer = "лонги → шортам" if rate >= 0 else "шорты → лонгам"
            rows.append([
                name[:8],
                f"{rate:+.4f}%",
                f"{apr:+.2f}%",
                payer,
            ])
        return "\n".join([
            f"💰 <b>FUNDING · {_esc(base)} — кто кому платит</b>",
            f"<i>{_fmt_utc_short(time.time())} · кэш {self.settings.funding_cache_minutes:.0f} мин</i>",
            "",
            "<pre>" + _mono_table(["Биржа", "Funding/8ч", "≈ APR", "Кто платит"], rows) + "</pre>",
            "",
            "Сигнал S→F (шорт перп): при funding &gt; 0 шорт ПЛУЩЕ ещё и за funding-платежи.",
            "Сигнал F→S (лонг перп): при funding &gt; 0 лонг платит — учти в расчёте.",
        ])

    def _build_signals_log_message(self) -> str:
        """Журнал событий on_demand-режима: где спред был ≥ порога."""
        now = time.time()
        if not self._events:
            return (
                "📩 <b>События (спред ≥ порога) — за сессию</b>\n\n"
                f"Пока пусто: сканер следит за порогом {self.settings.min_spread_percent:.2f}% "
                f"и фиксирует события в /signals (без отправки).\n\n"
                "Журнал в памяти и сбрасывается при перезапуске."
            )
        rows = []
        for event in list(self._events)[-20:][::-1]:
            rows.append([
                datetime.fromtimestamp(event.created_at, tz=timezone.utc).strftime("%d.%m %H:%M"),
                event.base[:8],
                event.direction.replace(">", "→"),
                f"{event.net_spread_percent:+.2f}%",
                f"{event.spot_exchange[:7]} → {event.futures_exchange[:7]}",
                _fmt_usd_human(event.min_notional_usd),
            ])
        return "\n".join([
            "📩 <b>СОБЫТИЯ ЗА СЕССИЮ</b> — где чистый спред был ≥ порога",
            f"<i>порог {self.settings.min_spread_percent:.2f}% · всего {self.stats['events_recorded']} · дубликаты на пару: кулдаун {self.settings.cooldown_minutes:.0f} мин</i>",
            "",
            "<pre>" + _mono_table(
                ["Время", "МОНТА", "DIR", "NET", "МАРШРУТ", "ЛИКВ"], rows
            ) + "</pre>",
            "",
            f"🕒 Сейчас: {datetime.fromtimestamp(now, tz=timezone.utc).strftime('%d.%m %H:%M')} UTC — актуальные спреды: /top",
        ])

    def _build_calc_message(self, base: str, amount_usd: float) -> str:
        """Калькулятор профита для указанной базы и суммы."""
        by_base = self._collect_opportunities(threshold=float("-inf"), direction=DIR_SPOT_TO_FUT)
        opps = by_base.get(base, [])
        if not opps:
            return (
                f"🧮 <b>Калькулятор: {_esc(base)} / ${amount_usd:.0f}</b>\n\n"
                f"Нет свежих данных по {base}. Попробуй /coin {base} или /top."
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
            "3. Профит при мгновенном закрытии — плюс funding каждые 8ч, пока хедж открыт",
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
# Форматирование сообщений (HTML для Telegram)
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


def _fmt_usd_human(value: float) -> str:
    """Компактный USDT для таблиц: $12.3k, $1.2M."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1000:.1f}k"
    if value >= 1:
        return f"${value:.0f}"
    return f"${value:.2f}"


def _fmt_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")


def _fmt_utc_short(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%d.%m %H:%M UTC")


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


def _mono_table(headers: list[str], rows: list[list[str]]) -> str:
    """
    Моноширинная таблица в столбиках для Telegram (<pre>).
    Ширина столбца = максимум из заголовка и ячеек.
    """
    if not rows:
        rows = [["—"] * len(headers)]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def render(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    lines = [render(list(headers)), "  ".join("-" * w for w in widths)]
    lines += [render(list(row)) for row in rows]
    return "\n".join(lines)


def format_signal_message(
    opportunity: Opportunity,
    alternatives: list[Opportunity],
    settings: Settings,
    *,
    cooldown_until: float,
) -> str:
    """
    Детальный сигнал о связке (по запросу /signal или auto-push).
    Цены/ликвидность — реальные из стакана.
    """
    o = opportunity
    s = settings

    def profit(usd: float) -> float:
        return usd * o.net_spread_percent / 100.0

    if o.is_spot_first:
        scheme = "long спот / short перп — без риска направления"
        fire = " 🔥🔥🔥" if o.net_spread_percent >= s.min_spread_percent + 2 else " 🔥" if o.net_spread_percent >= s.min_spread_percent else ""
    else:
        scheme = "long перп / short спот — без риска направления (нужен инвентарь)"
        fire = ""

    lines = [
        f"🚀 <b>АРБИТРАЖНАЯ СВЯЗКА · {_esc(o.base)}/USDT{fire}</b>",
        f"<i>Направление: {_esc(DIRECTION_LABELS[o.direction])}</i>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💎 <b>Чистый спред: {o.net_spread_percent:+.2f}%</b> (после комиссий {s.total_fee_percent:.2f}%)",
        f"📊 Гросс-спред: <b>{o.gross_spread_percent:+.2f}%</b>",
        "",
    ]

    if o.is_spot_first:
        lines += [
            f"📥 <b>Купить спот:</b> {_esc(o.spot_exchange)} — <code>{_fmt_price(o.spot_ask)}</code> USDT",
            f"   Пара: {_esc(o.spot_symbol)} | Глубина: {_fmt_usd(o.spot_notional_usd)}",
            f"📤 <b>Продать фьючерс (шорт):</b> {_esc(o.futures_exchange)} — <code>{_fmt_price(o.futures_bid)}</code> USDT",
            f"   Пара: {_esc(o.futures_symbol)} | Глубина: {_fmt_usd(o.futures_notional_usd)}",
        ]
    else:
        lines += [
            f"📥 <b>Купить фьючерс (лонг):</b> {_esc(o.futures_exchange)} — <code>{_fmt_price(o.futures_ask)}</code> USDT",
            f"   Пара: {_esc(o.futures_symbol)} | Глубина: {_fmt_usd(o.futures_notional_usd)}",
            f"📤 <b>Продать спот:</b> {_esc(o.spot_exchange)} — <code>{_fmt_price(o.spot_bid)}</code> USDT",
            f"   Пара: {_esc(o.spot_symbol)} | Глубина: {_fmt_usd(o.spot_notional_usd)}",
            f"   ⚠️ Нужен инвентарь {_esc(o.base)} на {_esc(o.spot_exchange)} (или перенос USDT+покупка)",
        ]

    lines += [
        "",
        f"💳 Комиссии: {s.spot_taker_fee_percent:.2f}% спот + {s.futures_taker_fee_percent:.2f}% фьючерс = {s.total_fee_percent:.2f}%",
    ]
    if o.funding_rate_percent is not None:
        holder = "шорт" if o.is_spot_first else "лонг"
        gets = "получает" if o.funding_rate_percent >= 0 else "платит"
        apr = o.funding_rate_percent * 3 * 365.0
        lines.append(
            f"💰 Funding {_esc(o.futures_exchange)}: {o.funding_rate_percent:+.4f}%/8ч — "
            f"{holder} {gets} (≈{apr:+.1f}% APR)"
        )
    lines += [
        f"📡 Возраст котировок: ≤ {o.data_age_seconds:.0f}с | Свежие данные из стакана",
        f"🔁 Схема: <b>{scheme}</b>",
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
    if o.is_spot_first:
        lines += [
            "",
            "📋 <b>ПОШАГОВЫЙ ПЛАН:</b>",
            f"1️⃣ Купи {_esc(o.base)} на <b>споте {_esc(o.spot_exchange)}</b> по ~<code>{_fmt_price(o.spot_ask)}</code> USDT",
            f"   └─ Тип ордера: LIMIT или MARKET, объём ≥ {_fmt_usd(o.spot_notional_usd)}",
            f"2️⃣ Одновременно открой <b>SHORT</b> на перпетуале <b>{_esc(o.futures_exchange)}</b> по ~<code>{_fmt_price(o.futures_bid)}</code>",
            f"   └─ Плечо: 1x-3x изолированно, хедж-мод, объём = спот-объёму",
            f"3️⃣ Держи хедж: цена {o.base} может идти куда угодно — P&L от цены ≈ 0",
            f"4️⃣ Закрой при схождении: продай спот + выкупи фьючерс, забери {o.net_spread_percent:+.2f}%",
            f"   └─ Или держи для funding: если funding положительный, шорт получает выплаты каждые 8ч",
        ]
    else:
        lines += [
            "",
            "📋 <b>ПОШАГОВЫЙ ПЛАН:</b>",
            f"1️⃣ Открой <b>LONG</b> на перпетуале <b>{_esc(o.futures_exchange)}</b> по ~<code>{_fmt_price(o.futures_ask)}</code>",
            f"   └─ Плечо: 1x-3x изолированно, объём = спот-объёму",
            f"2️⃣ Одновременно <b>продай {_esc(o.base)} на споте {_esc(o.spot_exchange)}</b> по ~<code>{_fmt_price(o.spot_bid)}</code>",
            f"   └─ Нужен инвентарь монеты; объём ≥ {_fmt_usd(o.spot_notional_usd)}",
            f"3️⃣ Держи хедж: LONG перп + SHORT спот — P&L от цены ≈ 0",
            f"4️⃣ Закрой при схождении: выкупи лонг + купи спот обратно, забери {o.net_spread_percent:+.2f}%",
        ]

    # Альтернативы
    if alternatives:
        alt_lines = "\n".join(
            f"   • {a.direction.replace('>', '→')} {_esc(a.spot_exchange)} → {_esc(a.futures_exchange)}: "
            f"{a.net_spread_percent:+.2f}% ({_fmt_price(a.buy_price)} → {_fmt_price(a.sell_price)})"
            for a in alternatives
        )
        lines += ["", "🔀 <b>Другие возможности:</b>", alt_lines]

    # Мета
    lines += ["", "━━━━━━━━━━━━━━━━━━━━", f"🕒 {_fmt_utc(o.created_at)}"]
    if cooldown_until and cooldown_until > 0:
        lines.append(f"⏳ Повторный сигнал по {_esc(o.base)} не раньше {_fmt_utc(cooldown_until)}")
    lines += [
        "",
        "⚠️ <b>Риски:</b> проверь ликвидность, funding rate, комиссии вывода, проскальзывание.",
        "Бот не торгует — только находит возможности. Решение за тобой.",
        "",
        "<i>💡 /coin BTC — все цены по биржам · /calc BTC 1000 — профит на твой депозит · "
        "/funding BTC — funding · /guide — как торговать</i>",
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

    if settings.signal_mode == "on_demand":
        signal_block = [
            "🔕 <b>Режим: сигналы ТОЛЬКО ПО ЗАПРОСУ</b> — бот ничего не присылает сам.",
            "   Спроси у него: /top (таблица спредов) или /signal (лучшая связка).",
            "   (авто-push включается переменной SIGNAL_MODE=auto)",
        ]
    else:
        signal_block = [
            "📣 <b>Режим: авто-push сигналов</b> при чистом спреде ≥ порога",
            f"   (кулдаун {settings.cooldown_minutes:.0f} мин/пара, лимит {settings.max_signals_per_scan}/цикл)",
        ]

    lines = [
        "✅ <b>Сканер арбитражных связок запущен</b> — профессиональный режим",
        "",
        f"🏦 Биржи: {_esc(', '.join(e.upper() for e in settings.exchanges))}",
        f"📡 Режим сбора: {_esc(mode)} — реальные цены из стаканов",
        f"🎯 Пар: {len(bases)} ({_esc(source)})",
        f"🚦 Порог сигнала: чистый спред ≥ {settings.min_spread_percent:.2f}%",
        f"💳 Комиссии: {settings.spot_taker_fee_percent:.2f}% + "
        f"{settings.futures_taker_fee_percent:.2f}% = {settings.total_fee_percent:.2f}%",
        "",
        *signal_block,
        "",
        "📋 <b>Что я делаю:</b>",
        "• Живу в фоне и держу свежие стаканы: спот USDT + перп USDT на каждой бирже",
        "• Считаю оба направления: S→F (купить спот/шорт перп) и F→S (лонг перп/продать спот)",
        "• Чистый спред = (цена продажи − цена покупки) / цена покупки × 100 − комиссии",
        "• По запросу показываю таблицы: где дёшево, где дорого, сколько заберёшь",
        "",
        "💡 <b>Основные команды:</b>",
        "/top — таблица топ-спредов (где купить/продать, цены, NET, ликвидность)",
        "/signal — лучшая связка прямо сейчас (детально, с планом)",
        "/coin BTC — все цены BTC по биржам + лучшая связка + план действий",
        "/coins — список монет, которые сканируются (по объёму 24ч)",
        "/funding BTC — funding-рейты по биржам (кто кому платит)",
        "/calc BTC 1000 — сколько заработаешь с $1000",
        "/signals — события: где спред был ≥ порога за сессию",
        "/help — все команды и как всё читать",
        "",
        "<i>Схема хеджа: LONG спот + SHORT перп = без риска направления. "
        "Без API-ключей, только публичные данные.</i>",
    ]
    if dead_labels:
        lines += ["", f"⚠️ Недоступны: {_esc(', '.join(dead_labels))} — бот работает с остальными"]
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
        f"📊 Сканов: {scanner.stats['scans']} · Сигналов (auto): {scanner.stats['signals_sent']} "
        f"(подавлено кулдауном: {scanner.stats['signals_suppressed_cooldown']})",
        f"📩 Событий ≥ порога (on_demand): {scanner.stats['events_recorded']}",
        f"📡 Свежих стаканов: {fresh}/{total} | Проверено комбинаций: {scanner.stats['combinations_checked']}",
        f"🔥 Лучший чистый спред сейчас: {best}",
        f"⏳ Пар в кулдауне: {active_cooldowns} | Пар отслеживается: {len(scanner.bases)}",
        "",
        "<i>Актуальные спреды: /top · лучшая связка: /signal</i>",
    ])


def format_status_message(scanner: ArbitrageScanner) -> str:
    """Подробный статус по команде /status: heartbeat + состояние бирж."""
    header = format_heartbeat_message(scanner)
    side_lines = ["", "🏦 <b>Биржи — детально:</b>"]
    for side in scanner.spot_sides + scanner.futures_sides:
        if side.alive:
            fresh, total = side.fresh_quotes_count()
            mode = "⚡ WS" if side.mode == "ws" else "🔄 REST"
            side_lines.append(
                f"   ✅ {_esc(side.label)}: {len(side.symbols)} пар, {mode}, "
                f"свежих {fresh}/{total}, ошибок {side.error_count}"
            )
        else:
            side_lines.append(f"   ❌ {_esc(side.label)}: недоступна")
    side_lines += [
        "",
        f"⚙️ Конфиг: режим {scanner.settings.signal_mode}, порог "
        f"{scanner.settings.min_spread_percent:.2f}%, комиссии "
        f"{scanner.settings.total_fee_percent:.2f}%, кулдаун "
        f"{scanner.settings.cooldown_minutes:.0f}м, "
        f"режим данных {'WS+REST' if scanner.settings.use_websocket else 'REST'}",
    ]
    return "\n".join([header] + side_lines)


def format_help_message(settings: Settings) -> str:
    return "\n".join([
        "🤖 <b>Сканер арбитража SPOT ↔ FUTURES — справка</b>",
        "",
        "<b>Как это работает:</b>",
        f"Держу живые стаканы {len(settings.exchanges)} бирж "
        f"({_esc(', '.join(e.upper() for e in settings.exchanges))}) и считаю ЧИСТЫЙ спред:",
        "• S→F: купить спот по min-ask + шорт перп по max-bid",
        "• F→S: лонг перп по min-ask + продать спот по max-bid",
        f"• NET = (продажа − покупка) / покупка × 100 − комиссии ({settings.total_fee_percent:.2f}%)",
        "",
        "🔕 <b>По умолчанию бот НИЧЕГО не присылает сам — только по запросу</b> "
        f"(порог «сигнала»: {settings.min_spread_percent:.2f}%).",
        "Авто-push: SIGNAL_MODE=auto в переменных окружения.",
        "",
        "<b>📊 Команды:</b>",
        "/top [N] — таблица топ-N спредов: где купить, где шортить, NET, ликвидность",
        "/top fs [N] — то же для обратного направления (F→S)",
        "/signal [COIN] — лучшая связка прямо сейчас (детально + план + funding)",
        "/coin BTC (алиас /price BTC) — РАЗБОР: все цены BTC по биржам, лучшая связка, план действий",
        "/coins [PAGE] — список монет (по объёму 24ч) + спред по каждой",
        "/funding BTC — funding по биржам: кто кому платит, APR",
        "/calc BTC 1000 — профит на свой депозит",
        "/signals — события: где спред был ≥ порога за сессию",
        "/status — аптайм, статистика, лучший спред",
        "/exchanges — детальный статус бирж (WS/REST, ошибки)",
        "/guide — гайд: как исполнять связку, риски",
        "/strategy — стратегии и математика",
        "/help — эта справка",
        "",
        "<b>Как читать сигнал:</b>",
        "📥 КУПИТЬ — биржа, где дешевле всего (цена ask)",
        "📤 ШОРТ/ПРОДАТЬ — биржа, где дороже всего (цена bid)",
        "💎 NET — твой % после комиссий (теоретический профит)",
        "💧 ЛИКВ — глубина на лучшей цене: не входи больше неё",
        "",
        "<b>🔁 Хедж:</b> LONG спот + SHORT перп (или наоборот) = риска направления нет. "
        "Цена может лететь куда угодно — ты забираешь только разницу.",
        "",
        "Просто нажми кнопки ниже 👇",
    ])


def format_guide_message() -> str:
    return "\n".join([
        "📚 <b>ГАЙД: МЕЖБИРЖЕВОЙ АРБИТРАЖ SPOT ↔ FUTURES — ПОШАГОВО</b>",
        "",
        "<b>1. Что это?</b>",
        "Спот на одной бирже торгуется дешевле, чем фьючерс на другой (или наоборот). "
        "Покупаем дёшево, продаём дорого одновременно — разница (спред) заперта в хедже.",
        "",
        "<b>2. Зачем хедж (long спот / short перп)?</b>",
        "LONG спот + SHORT перп = дельта-нейтральность. Если BTC растёт — спот в плюсе, "
        "шорт в минусе, суммарно 0 + запертый спред. Падает — наоборот. "
        "Ты зарабатываешь на РАЗНИЦЕ ЦЕН, а не на движении рынка.",
        "",
        "<b>3. Пошагово (пример):</b>",
        "Сигнал: BTC S→F +2.85% — купить MEXC @ 100.00, шорт Bybit @ 103.00",
        "",
        "Шаг 1️⃣ <b>Купи спот</b>",
        "• MEXC → Спот → BTC/USDT",
        "• Купи на $1000 по ~100.00 (LIMIT для лучшей цены, MARKET за скорость)",
        "• Комиссия ~0.10% = $1",
        "",
        "Шаг 2️⃣ <b>Открой SHORT на перпетуале</b>",
        "• Bybit → Перпетуалы → BTCUSDT",
        "• SHORT на $1000 по ~103.00, плечо 1x–3x, ИЗОЛИРОВАННАЯ маржа",
        "• Комиссия ~0.05% = $0.50",
        "",
        "Шаг 3️⃣ <b>Ты в хедже</b>",
        "• Профит зафиксирован: +2.85% = $28.5 с $1000",
        "• Цена BTC больше не важна. Остаточный риск — ликвидация (поэтому 1x–3x)",
        "",
        "Шаг 4️⃣ <b>Выход</b>",
        "• Быстрый: когда спред сошёлся — продай спот + выкупи шорт, забери $28.5",
        "• Долгий: держи, пока funding положительный — шорт ДOПОЛНИТЕЛЬНО получает "
        "выплаты каждые 8ч (видишь в /funding BTC)",
        "",
        "<b>4. Обратное направление F→S (backwardation):</b>",
        "Спот дороже фьючерса: лонг перп + продажа спота. Нужен инвентарь монеты "
        "на спот-бирже. Бот считает его — /top fs и /coin BTC показывают оба направления.",
        "",
        "<b>5. Где всё смотреть в боте:</b>",
        "/top — таблица: где купить, где шортить, NET, ликвидность",
        "/coin BTC — все цены BTC по биржам + лучшая связка + план",
        "/signal — лучшая связка прямо сейчас (по запросу, без спама)",
        "/coins — список монет, /funding BTC — funding, /calc BTC 1000 — профит",
        "",
        "<b>6. Риски и как их гасить:</b>",
        "• Проскальзывание — ставь лимитки, не входи больше ЛИКВ из сигнала",
        "• Funding — при отрицательном шорт платит; смотри /funding до входа",
        "• Ликвидация — плечо 1x–3x, изолированная маржа, не доводи до мели",
        "• Комиссии вывода — держи USDT на обеих биржах заранее, не гоняй монеты",
        "• Биржа легла — бот покажет в /status; исполняй на живых сторонах",
        "",
        "<b>7. Советы профи:</b>",
        "• Начинай с $100–500 — прогони механику целиком",
        "• Лимитные ордера дают лучшую цену, маркет — скорость",
        "• Спред > 5% почти всегда низколиквидная монета — проверяй ЛИКВ",
        "• Следи за funding: +0.01%/8ч ≈ +10% APR сверху спреда",
        "",
        "💡 <i>Бот находит возможности — исполняет сделки ты. Решение и риск — твои.</i>",
        "📊 Начни с /top и /coin BTC!",
    ])


def format_strategy_message(settings: Settings) -> str:
    return "\n".join([
        "🧠 <b>СТРАТЕГИИ И МАТЕМАТИКА АРБИТРАЖА</b>",
        "",
        "<b>Формулы:</b>",
        "S→F: Gross = (FutBid − SpotAsk) / SpotAsk × 100%",
        "F→S: Gross = (SpotBid − FutAsk) / FutAsk × 100%",
        f"Net = Gross − Fees ({settings.spot_taker_fee_percent:.2f}% + "
        f"{settings.futures_taker_fee_percent:.2f}% = {settings.total_fee_percent:.2f}%)",
        f"Сигнал: Net ≥ {settings.min_spread_percent:.2f}%",
        "",
        "<b>Пример S→F:</b>",
        "SpotAsk MEXC = 100.00, FutBid Bybit = 103.00",
        "Gross = (103−100)/100×100 = +3.00%",
        f"Net = 3.00% − {settings.total_fee_percent:.2f}% = {3.0 - settings.total_fee_percent:.2f}%",
        f"Депозит $1000 → профит ${1000 * (3.0 - settings.total_fee_percent) / 100:.2f}",
        "",
        "<b>Типы арбитражей (что считает бот / что нет):</b>",
        "1️⃣ <b>Spot → Futures (S→F)</b> ✅ основной:",
        "   Купить спот, шорт перп на другой бирже. Работает в contango (фьючерс дороже).",
        "   Бонус: при положительном funding шорт получает выплаты каждые 8ч.",
        "",
        "2️⃣ <b>Futures → Spot (F→S)</b> ✅ /top fs, /coin показывает:",
        "   Лонг перп + продажа спота. Работает в backwardation (спот дороже).",
        "   Требует инвентарь монеты на спот-бирже.",
        "",
        "3️⃣ <b>Spot → Spot (межбиржевой)</b> ⚠️ бот не считает:",
        "   Купил спот на A, продал спот на B. Нужен перевод монет по сети: "
        "время + комиссия сети + риск движения цены за время перевода.",
        "",
        "4️⃣ <b>Futures → Futures</b> ⚠️ бот не считает:",
        "   Лонг перп на одной, шорт на другой. Не нужен спот, только маржа, "
        "но funding с двух сторон съедает профит.",
        "",
        "<b>Funding Rate — второй источник дохода:</b>",
        "Каждые 8ч лонги и шорты обмениваются выплатой. При funding +0.01%/8ч шорт "
        "получает 0.03%/день ≈ 10% APR. Смотри /funding BTC: знак показывает, "
        "кто кому платит.",
        "",
        "<b>Риски и снижение:</b>",
        "• Проскальзывание → лимитки + чтение ЛИКВ в сигнале",
        "• Funding → проверяй /funding до входа",
        "• Ликвидация → 1x–3x, изолированная маржа",
        "• Комиссии вывода → держи USDT на обеих биржах",
        "• Сбой биржи → /status, торгуй на живых",
        "",
        "<b>Вдохновлено сканерами с GitHub</b> (ArbitrageScanner, crypto-futures-arbitrage-scanner, "
        "OKX spot-futures bot): реальные стаканы WS+REST, оба направления, funding, "
        "таблицы с ценами, исполнение — руками (без API-ключей и без риска за твои средства).",
        "",
        f"⚙️ Текущий порог: {settings.min_spread_percent:.2f}%, комиссии "
        f"{settings.total_fee_percent:.2f}%, кулдаун {settings.cooldown_minutes:.0f}м",
        "",
        "<i>Пошаговое исполнение — /guide · пример на реальных ценах — /coin BTC</i>",
    ])