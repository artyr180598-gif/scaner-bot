"""
Offline-тесты логики сканера (без сети): формула спреда, фильтры,
кулдаун, форматирование сообщений, парсинг конфигурации.

Запуск из корня проекта:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import ccxt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, is_scannable_base                     # noqa: E402
from forge import ForgeSnapshot                                    # noqa: E402
from scanner import (                                              # noqa: E402
    DIR_FUT_TO_SPOT,
    DIR_SPOT_TO_FUT,
    EXCHANGE_REGISTRY,
    ArbitrageScanner,
    BookQuote,
    ExchangeSide,
    Opportunity,
    _walk_book_vwap,
    format_forge_one,
    format_signal_message,
    format_startup_message,
)
from telegram_bot import (                                         # noqa: E402
    MAIN_MENU_KEYBOARD,
    TelegramCommandListener,
    TelegramNotifier,
)


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def make_settings(**overrides) -> Settings:
    defaults = dict(
        telegram_bot_token=None,
        chat_ids=(),
        min_spread_percent=2.0,
        cooldown_minutes=15.0,
        spot_taker_fee_percent=0.1,
        futures_taker_fee_percent=0.05,
        max_spread_percent=30.0,
        price_deviation_max_percent=50.0,
        max_book_spread_percent=10.0,
        # Легаси-тесты ниже проверяют СТАРУЮ семантику плоского порога v2.
        # Поведение v3 (adaptive) покрыто в TestQuantEngine.
        strategy_mode="fixed",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_quote(bid: float, ask: float, bid_qty: float = 10.0, ask_qty: float = 10.0,
               age_seconds: float = 0.0) -> BookQuote:
    return BookQuote(
        bid=bid, bid_qty=bid_qty, ask=ask, ask_qty=ask_qty,
        bid_notional_usd=bid * bid_qty, ask_notional_usd=ask * ask_qty,
        timestamp=time.time() - age_seconds,
    )


def make_side(exchange_id: str, market_type: str, quotes: dict[str, BookQuote]) -> ExchangeSide:
    """Сторона биржи без реального подключения к ccxt (для offline-тестов)."""
    spec = EXCHANGE_REGISTRY[exchange_id]
    side = ExchangeSide(make_settings(), spec, market_type, exchange=None)
    side.alive = True
    suffix = "/USDT" if market_type == "spot" else "/USDT:USDT"
    side.symbol_by_base = {base: f"{base}{suffix}" for base in quotes}
    side.symbols = list(side.symbol_by_base.values())
    # Котировки в кэше ключуются unified-символом, а не базой.
    side.quotes = {side.symbol_by_base[base]: quote for base, quote in quotes.items()}
    return side


def make_scanner(settings: Settings, spot_sides, futures_sides, notifier) -> ArbitrageScanner:
    scanner = ArbitrageScanner(settings, notifier)
    scanner.spot_sides = list(spot_sides)
    scanner.futures_sides = list(futures_sides)
    scanner.bases = sorted(
        {b for s in spot_sides for b in s.symbol_by_base}
        & {b for s in futures_sides for b in s.symbol_by_base}
    )
    return scanner


class FakeNotifier:
    def __init__(self, deliver: bool = True):
        self.deliver = deliver
        self.messages: list[str] = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def send_html(self, html: str, reply_markup: dict | None = None) -> bool:
        if self.deliver:
            self.messages.append(html)
        return self.deliver


# ---------------------------------------------------------------------------
# Формула спреда и фильтры
# ---------------------------------------------------------------------------

class TestSpreadEvaluation(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()

    def test_net_spread_formula(self):
        """Spread = (FutBid - SpotAsk)/SpotAsk*100 - TotalCommissions."""
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99.0, ask=100.0)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103.0, ask=104.0)})
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())

        pairs = scanner._evaluate()
        self.assertEqual(len(pairs), 1)
        best, alternatives = pairs[0]

        self.assertAlmostEqual(best.gross_spread_percent, 3.0, places=6)
        self.assertAlmostEqual(best.net_spread_percent, 3.0 - 0.15, places=6)
        self.assertEqual(best.base, "BTC")
        self.assertEqual(best.spot_exchange, "MEXC")
        self.assertEqual(best.futures_exchange, "Bybit")
        self.assertEqual(best.spot_ask, 100.0)
        self.assertEqual(best.futures_bid, 103.0)
        self.assertFalse(best.same_exchange)

    def test_below_threshold_is_filtered(self):
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99.0, ask=100.0)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=101.0, ask=104.0)})
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())
        self.assertEqual(scanner._evaluate(), [])  # 1% - 0.15% = 0.85% < 2%

    def test_stale_quotes_ignored(self):
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100, age_seconds=999)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=110, ask=111)})
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())
        self.assertEqual(scanner._evaluate(), [])

    def test_dead_side_ignored(self):
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        spot.alive = False
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=110, ask=111)})
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())
        self.assertEqual(scanner._evaluate(), [])

    def test_same_exchange_forbidden_by_default(self):
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        fut = make_side("mexc", "futures", {"BTC": make_quote(bid=110, ask=111)})
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())
        self.assertEqual(scanner._evaluate(), [])

    def test_same_exchange_allowed_via_flag(self):
        settings = make_settings(allow_same_exchange=True)
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        fut = make_side("mexc", "futures", {"BTC": make_quote(bid=110, ask=111)})
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        pairs = scanner._evaluate()
        self.assertEqual(len(pairs), 1)
        self.assertTrue(pairs[0][0].same_exchange)

    def test_notional_filter(self):
        settings = make_settings(min_notional_usd=500.0)
        # На лучшей цене спота всего 100$ объёма — связка отфильтруется.
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100, ask_qty=1.0)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=110, ask=111, bid_qty=50.0)})
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        self.assertEqual(scanner._evaluate(), [])

    def test_best_route_per_base_and_alternatives(self):
        """По одной паре выбирается лучший маршрут, остальные — альтернативы."""
        spot_mexc = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        spot_okx = make_side("okx", "spot", {"BTC": make_quote(bid=99, ask=99.5)})
        fut_bybit = make_side("bybit", "futures", {"BTC": make_quote(bid=103, ask=104)})
        fut_okx = make_side("okx", "futures", {"BTC": make_quote(bid=102.5, ask=103.5)})
        scanner = make_scanner(self.settings, [spot_mexc, spot_okx], [fut_bybit, fut_okx],
                               FakeNotifier())
        pairs = scanner._evaluate()
        self.assertEqual(len(pairs), 1)
        best, alternatives = pairs[0]
        # Лучшая связка: покупаем дешевле (OKX, 99.5), продаём дороже (Bybit, 103).
        self.assertEqual(best.spot_exchange, "OKX")
        self.assertEqual(best.futures_exchange, "Bybit")
        self.assertTrue(len(alternatives) >= 2)
        nets = [best.net_spread_percent] + [a.net_spread_percent for a in alternatives]
        self.assertEqual(nets, sorted(nets, reverse=True))

    def test_fees_change_result(self):
        """Комиссии настраиваемые: рост комиссий душит пограничный спред."""
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=102.3, ask=104)})
        # гросс 2.3%: при комиссиях 0.15% -> 2.15% (сигнал), при 1.0% -> 1.3% (нет).
        scanner_cheap = make_scanner(make_settings(), [spot], [fut], FakeNotifier())
        self.assertEqual(len(scanner_cheap._evaluate()), 1)
        scanner_expensive = make_scanner(
            make_settings(spot_taker_fee_percent=0.5, futures_taker_fee_percent=0.5),
            [spot], [fut], FakeNotifier(),
        )
        self.assertEqual(scanner_expensive._evaluate(), [])


# ---------------------------------------------------------------------------
# Кулдаун (anti-spam)
# ---------------------------------------------------------------------------

class TestCooldown(unittest.TestCase):
    """Кулдауны push-сигналов работают в режиме SIGNAL_MODE=auto."""

    def _scanner_with_signal(self, notifier, **settings_overrides):
        settings = make_settings(signal_mode="auto", **settings_overrides)
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=110, ask=111)})
        return make_scanner(settings, [spot], [fut], notifier)

    def test_no_repeat_within_cooldown(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)
        pairs = scanner._evaluate()

        import asyncio
        asyncio.run(scanner._process_signals(pairs))
        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(scanner.stats["signals_sent"], 1)

        # Второй цикл сразу после — сигнал подавлен кулдауном.
        asyncio.run(scanner._process_signals(scanner._evaluate()))
        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(scanner.stats["signals_suppressed_cooldown"], 1)

    def test_signal_returns_after_cooldown_expires(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)

        import asyncio
        asyncio.run(scanner._process_signals(scanner._evaluate()))
        self.assertEqual(len(notifier.messages), 1)

        # Имитируем истечение кулдауна.
        scanner._cooldown_until["BTC"] = time.time() - 1.0
        asyncio.run(scanner._process_signals(scanner._evaluate()))
        self.assertEqual(len(notifier.messages), 2)

    def test_failed_delivery_sets_short_retry_cooldown(self):
        notifier = FakeNotifier(deliver=False)
        scanner = self._scanner_with_signal(notifier)

        import asyncio
        asyncio.run(scanner._process_signals(scanner._evaluate()))
        self.assertEqual(scanner.stats["signals_sent"], 0)
        # Кулдаун после сбоя — короткий (60с), а не COOLDOWN_MINUTES.
        remaining = scanner._cooldown_until["BTC"] - time.time()
        self.assertLess(remaining, 70.0)
        self.assertGreater(remaining, 0.0)

    def test_max_signals_per_scan(self):
        notifier = FakeNotifier()
        settings = make_settings(signal_mode="auto", max_signals_per_scan=1)
        quotes = {b: make_quote(bid=99, ask=100) for b in ("BTC", "ETH", "SOL")}
        fut_quotes = {b: make_quote(bid=110, ask=111) for b in ("BTC", "ETH", "SOL")}
        scanner = make_scanner(settings, [make_side("mexc", "spot", quotes)],
                               [make_side("bybit", "futures", fut_quotes)], notifier)

        import asyncio
        asyncio.run(scanner._process_signals(scanner._evaluate()))
        self.assertEqual(len(notifier.messages), 1)


# ---------------------------------------------------------------------------
# Форматирование сообщений
# ---------------------------------------------------------------------------

class TestMessageFormatting(unittest.TestCase):
    def _opportunity(self, **overrides) -> Opportunity:
        defaults = dict(
            base="BTC", spot_exchange="MEXC", futures_exchange="Bybit",
            spot_symbol="BTC/USDT", futures_symbol="BTC/USDT:USDT",
            spot_ask=100.0, futures_bid=103.0,
            gross_spread_percent=3.0, net_spread_percent=2.85,
            spot_notional_usd=12_345.0, futures_notional_usd=9_800.0,
            same_exchange=False, created_at=1_800_000_000.0,
        )
        defaults.update(overrides)
        return Opportunity(**defaults)

    def test_signal_message_contains_details(self):
        settings = make_settings()
        alt = self._opportunity(spot_exchange="OKX", net_spread_percent=2.5)
        message = format_signal_message(
            self._opportunity(), [alt], settings, cooldown_until=1_800_000_900.0,
        )
        self.assertIn("BTC/USDT", message)
        self.assertIn("+2.85%", message)
        self.assertIn("MEXC", message)
        self.assertIn("Bybit", message)
        self.assertIn("100.00", message)       # цена покупки
        self.assertIn("103.00", message)       # цена продажи
        self.assertIn("+3.00%", message)       # гросс-спред
        self.assertIn("0.10%", message)        # комиссии
        self.assertIn("$12,345", message)      # ликвидность
        self.assertIn("OKX", message)          # альтернатива
        self.assertIn("не раньше", message)    # кулдаун
        self.assertNotIn("<b></b>", message)

    def test_small_price_formatting(self):
        settings = make_settings()
        opp = self._opportunity(
            base="PEPE", spot_ask=0.00001234, futures_bid=0.00001299,
        )
        message = format_signal_message(opp, [], settings, cooldown_until=0.0)
        self.assertIn("PEPE/USDT", message)
        self.assertIn("0.00001234", message)

    def test_startup_message(self):
        settings = make_settings()
        message = format_startup_message(settings, ["BTC", "ETH", "SOL"])
        self.assertIn("запущен", message)
        self.assertIn("MEXC", message)
        self.assertIn("2.00%", message)

    def test_dry_run_notifier(self):
        import asyncio
        notifier = TelegramNotifier(None, ())
        self.assertTrue(notifier.dry_run)
        self.assertTrue(asyncio.run(notifier.send_html("<b>test</b>")))


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.min_spread_percent, 2.0)
        self.assertEqual(settings.cooldown_minutes, 15.0)
        self.assertEqual(settings.spot_taker_fee_percent, 0.1)
        self.assertEqual(settings.futures_taker_fee_percent, 0.05)
        self.assertAlmostEqual(settings.total_fee_percent, 0.15)
        self.assertEqual(settings.price_deviation_max_percent, 15.0)
        self.assertEqual(settings.max_spread_percent, 8.0)
        self.assertEqual(settings.max_book_spread_percent, 2.0)
        self.assertEqual(settings.exchanges, ("mexc", "bybit", "okx", "binance"))
        self.assertTrue(settings.use_websocket)
        self.assertFalse(settings.allow_same_exchange)
        self.assertIsNone(settings.telegram_bot_token)
        self.assertEqual(settings.chat_ids, ())

    def test_env_parsing_and_aliases(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "abc",
            "CHAT_ID": "111, 222",
            "MIN_SPREAD_PERCENT": "1.5",
            "COOLDOWN_MINUTES": "30",
            "SPOT_TAKER_FEE_PERCENT": "0,075",  # запятая как разделитель
            "EXCHANGES": "mexc, bybit",
            "SYMBOLS": "btc, ETH",
            "USE_WEBSOCKET": "false",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.telegram_bot_token, "abc")
        self.assertEqual(settings.chat_ids, ("111", "222"))
        self.assertEqual(settings.min_spread_percent, 1.5)
        self.assertEqual(settings.cooldown_minutes, 30.0)
        self.assertAlmostEqual(settings.spot_taker_fee_percent, 0.075)
        self.assertEqual(settings.exchanges, ("mexc", "bybit"))
        self.assertEqual(settings.symbols, ("BTC", "ETH"))
        self.assertFalse(settings.use_websocket)

    def test_min_spread_legacy_alias(self):
        with mock.patch.dict(os.environ, {"MIN_SPREAD": "3.3"}, clear=True):
            self.assertEqual(Settings.from_env().min_spread_percent, 3.3)

    def test_invalid_values_raise(self):
        with mock.patch.dict(os.environ, {"MIN_SPREAD_PERCENT": "abc"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()
        with mock.patch.dict(os.environ, {"EXCHANGES": "kucoin,ftx"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()
        with mock.patch.dict(os.environ, {"COOLDOWN_MINUTES": "-5"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_is_scannable_base(self):
        self.assertTrue(is_scannable_base("BTC"))
        self.assertFalse(is_scannable_base("USDT"))     # стейблкоин
        self.assertFalse(is_scannable_base("BTCUP"))    # леверидж-токен
        self.assertFalse(is_scannable_base("BTC/USDT")) # не базовый актив
        self.assertFalse(is_scannable_base(""))
        self.assertFalse(is_scannable_base("1000PEPE"))
        self.assertFalse(is_scannable_base("1MBONK"))
        self.assertTrue(is_scannable_base("KAVA"))


# ---------------------------------------------------------------------------
# Отказоустойчивость потока данных: деградация WebSocket → REST
# ---------------------------------------------------------------------------

class StubWatchExchange:
    """
    Заглушка ccxt-инстанса ТОЛЬКО для тестов отказоустойчивости:
    watch_order_book всегда падает, fetch_order_book возвращает стакан.
    В продакшн-коде заглушек нет — данные всегда приходят с реальных бирж.
    """

    def __init__(self, raise_not_supported: bool = False):
        self.has = {"watchOrderBook": True}
        self.markets = {}
        self.watch_calls = 0
        self.fetch_calls = 0
        self.raise_not_supported = raise_not_supported

    async def watch_order_book(self, symbol, limit=None):
        self.watch_calls += 1
        if self.raise_not_supported:
            raise ccxt.NotSupported("watch_order_book not supported")
        raise ccxt.NetworkError("simulated connection reset")

    async def fetch_order_book(self, symbol, limit=None):
        self.fetch_calls += 1
        return {"bids": [[100.0, 2.0]], "asks": [[100.5, 4.0]], "timestamp": 0}

    async def close(self):
        pass


class TestFeedResilience(unittest.TestCase):
    def _make_side(self, stub) -> ExchangeSide:
        settings = make_settings(
            use_websocket=True,
            ws_fails_before_fallback=3,
            rest_throttle_seconds=0.0,
            rest_poll_interval_seconds=0.05,
        )
        side = ExchangeSide(settings, EXCHANGE_REGISTRY["mexc"], "spot", exchange=stub)
        side.alive = True
        side.symbol_by_base = {"BTC": "BTC/USDT"}
        side.symbols = ["BTC/USDT"]
        return side

    def test_ws_fail_streak_switches_to_rest(self):
        """Серия сбоев WebSocket переключает сторону на REST-опрос."""
        stub = StubWatchExchange()
        side = self._make_side(stub)
        self.assertEqual(side.mode, "ws")

        async def scenario():
            side.start()
            await asyncio.sleep(5.0)
            mode = side.mode
            watch_calls = stub.watch_calls
            fetch_calls = stub.fetch_calls
            quote = side.quotes.get("BTC/USDT")
            await side.stop()
            return mode, watch_calls, fetch_calls, quote

        mode, watch_calls, fetch_calls, quote = asyncio.run(scenario())
        self.assertEqual(mode, "rest")
        self.assertEqual(watch_calls, 3)          # переключились ровно на пороге
        self.assertGreaterEqual(fetch_calls, 1)   # REST-опрос заработал
        self.assertIsNotNone(quote)               # реальные (в тесте — стаб) цены в кэше
        self.assertEqual(quote.bid, 100.0)
        self.assertEqual(quote.ask, 100.5)

    def test_not_supported_switches_to_rest_immediately(self):
        stub = StubWatchExchange(raise_not_supported=True)
        side = self._make_side(stub)

        async def scenario():
            side.start()
            await asyncio.sleep(0.7)
            mode = side.mode
            watch_calls = stub.watch_calls
            fetch_calls = stub.fetch_calls
            await side.stop()
            return mode, watch_calls, fetch_calls

        mode, watch_calls, fetch_calls = asyncio.run(scenario())
        self.assertEqual(mode, "rest")
        self.assertEqual(watch_calls, 1)
        self.assertGreaterEqual(fetch_calls, 1)

    def test_rest_mode_without_exchange_object(self):
        """Без ccxt-инстанса (offline-тесты) сторона стартует в REST-режиме."""
        side = make_side("mexc", "spot", {})
        self.assertEqual(side.mode, "rest")


# ---------------------------------------------------------------------------
# Интерактивные команды Telegram: маршрутизация и обработчики
# ---------------------------------------------------------------------------

class FakeCommandTransport:
    """Стаб Telegram-транспорта для тестирования приёма команд."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.answered: list[tuple] = []

    async def send_html_to_chat(self, chat_id, html, reply_markup=None):
        self.sent.append((chat_id, html))
        return True

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append((callback_query_id, text))


class RecordingHandler:
    def __init__(self, name="cmd"):
        self.name = name
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, chat_id: str, args: str):
        self.calls.append((chat_id, args))
        return f"ответ {self.name}({args})"


def make_listener(transport, handlers, allowed=("111",)):
    return TelegramCommandListener(transport, handlers, allowed)


def message_update(update_id: int, chat_id: int, text: str) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def callback_update(update_id: int, chat_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {"id": "cbq1", "data": data, "message": {"chat": {"id": chat_id}}},
    }


class TestCommandRouting(unittest.TestCase):
    def test_command_message_dispatch(self):
        transport = FakeCommandTransport()
        status_handler = RecordingHandler("status")
        listener = make_listener(transport, {"status": status_handler})

        asyncio.run(listener._dispatch(message_update(1, 111, "/status")))
        self.assertEqual(status_handler.calls, [("111", "")])
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(transport.sent[0][0], "111")
        self.assertIn("ответ status", transport.sent[0][1])

    def test_command_with_args_and_bot_suffix(self):
        """/price@MyBot BTC → обработчик price с аргументом BTC."""
        transport = FakeCommandTransport()
        price_handler = RecordingHandler("price")
        listener = make_listener(transport, {"price": price_handler})

        asyncio.run(listener._dispatch(message_update(1, 111, "/price@MyBot BTC")))
        self.assertEqual(price_handler.calls, [("111", "BTC")])

    def test_callback_button_dispatch(self):
        transport = FakeCommandTransport()
        top_handler = RecordingHandler("top")
        listener = make_listener(transport, {"top": top_handler})

        asyncio.run(listener._dispatch(callback_update(1, 111, "top")))
        self.assertEqual(top_handler.calls, [("111", "")])
        self.assertEqual(transport.answered, [("cbq1", None)])  # кнопка подтверждена
        self.assertEqual(len(transport.sent), 1)

    def test_callback_with_argument(self):
        transport = FakeCommandTransport()
        price_handler = RecordingHandler("price")
        listener = make_listener(transport, {"price": price_handler})

        asyncio.run(listener._dispatch(callback_update(1, 111, "price:ETH")))
        self.assertEqual(price_handler.calls, [("111", "ETH")])

    def test_unauthorized_chat_ignored(self):
        transport = FakeCommandTransport()
        status_handler = RecordingHandler("status")
        listener = make_listener(transport, {"status": status_handler})

        asyncio.run(listener._dispatch(message_update(1, 999, "/status")))
        self.assertEqual(status_handler.calls, [])
        self.assertEqual(transport.sent, [])

    def test_unauthorized_callback_denied(self):
        transport = FakeCommandTransport()
        top_handler = RecordingHandler("top")
        listener = make_listener(transport, {"top": top_handler})

        asyncio.run(listener._dispatch(callback_update(1, 999, "top")))
        self.assertEqual(top_handler.calls, [])
        self.assertEqual(transport.sent, [])
        self.assertIn("Нет доступа", str(transport.answered))

    def test_plain_text_ignored(self):
        transport = FakeCommandTransport()
        help_handler = RecordingHandler("help")
        listener = make_listener(transport, {"help": help_handler})

        asyncio.run(listener._dispatch(message_update(1, 111, "привет, как дела?")))
        self.assertEqual(help_handler.calls, [])
        self.assertEqual(transport.sent, [])

    def test_unknown_command_falls_back_to_help(self):
        transport = FakeCommandTransport()
        help_handler = RecordingHandler("help")
        listener = make_listener(transport, {"help": help_handler})

        asyncio.run(listener._dispatch(message_update(1, 111, "/абракадабра")))
        self.assertEqual(help_handler.calls, [("111", "")])

    def test_handler_exception_returns_friendly_error(self):
        transport = FakeCommandTransport()

        async def broken(chat_id, args):
            raise RuntimeError("boom")

        listener = make_listener(transport, {"status": broken})
        asyncio.run(listener._dispatch(message_update(1, 111, "/status")))
        self.assertEqual(len(transport.sent), 1)
        self.assertIn("Не удалось выполнить команду", transport.sent[0][1])

    def test_command_parsing(self):
        parse = TelegramCommandListener._parse_command
        self.assertEqual(parse("/top"), ("top", ""))
        self.assertEqual(parse("/price BTC"), ("price", "BTC"))
        self.assertEqual(parse("/PRICE btc eth"), ("price", "btc eth"))
        self.assertEqual(parse("/status@arb_scanner_bot"), ("status", ""))


class TestCommandHandlers(unittest.TestCase):
    """Обработчики команд отдают реальные данные из кэша стаканов."""

    def _scanner(self, **settings_overrides) -> ArbitrageScanner:
        settings = make_settings(**settings_overrides)
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99.0, ask=100.0)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103.0, ask=104.0)})
        return make_scanner(settings, [spot], [fut], FakeNotifier())

    def test_top_command_shows_real_spreads(self):
        scanner = self._scanner()
        message = asyncio.run(scanner._cmd_top("111", ""))
        self.assertIn("BTC", message)
        self.assertIn("+2.85%", message)      # реальный расчёт по котировкам
        self.assertIn("MEXC", message)
        self.assertIn("Bybit", message)
        self.assertIn("🔥", message)          # спред над порогом 2.0%

    def test_top_command_below_threshold_still_listed(self):
        scanner = self._scanner(min_spread_percent=10.0)
        message = asyncio.run(scanner._cmd_top("111", ""))
        self.assertIn("+2.85%", message)      # /top показывает всё
        self.assertNotIn("🔥", message)       # но помечает, что порог не пройден

    def test_price_command_shows_real_quotes(self):
        scanner = self._scanner()
        message = asyncio.run(scanner._cmd_price("111", "btc"))  # регистронезависимо
        self.assertIn("BTC/USDT", message)
        self.assertIn("MEXC", message)
        self.assertIn("Bybit", message)
        self.assertIn("100.0000", message)    # реальный ask со стакана
        self.assertIn("103.0000", message)    # реальный bid со стакана

    def test_price_command_unknown_base(self):
        scanner = self._scanner()
        message = asyncio.run(scanner._cmd_price("111", "NOSUCHCOIN"))
        self.assertIn("нет свежих данных", message)

    def test_status_command(self):
        scanner = self._scanner()
        message = asyncio.run(scanner._cmd_status("111", ""))
        self.assertIn("Аптайм", message)
        self.assertIn("MEXC spot", message)
        self.assertIn("Bybit futures", message)
        self.assertIn("✅", message)

    def test_help_command(self):
        scanner = self._scanner()
        message = asyncio.run(scanner._cmd_help("111", ""))
        for command in ("/top", "/status", "/price", "/help", "/forge"):
            self.assertIn(command, message)

    def test_handlers_registry(self):
        scanner = self._scanner()
        handlers = scanner.telegram_handlers()
        for name in ("start", "help", "status", "top", "spreads", "price", "pulse", "forge"):
            self.assertIn(name, handlers)

    def test_forge_command_warmup(self):
        scanner = self._scanner()
        message = asyncio.run(scanner._cmd_forge("111", ""))
        self.assertIn("FORGE", message)
        self.assertIn("авто-вход", message.lower())
        card = asyncio.run(scanner._cmd_forge("111", "BTC"))
        self.assertIn("BTC", card)

    def test_forge_card_has_plan_and_stop(self):
        snap = ForgeSnapshot(
            symbol="SOL", n_bars=120, close=100.0, resid=0.04, vol=0.02,
            sma=90.0, stop=92.0, atr=2.0, above_sma=True, chandelier_ok=True,
            quiet=True, liquid=True, picked=True, entry=True,
        )
        msg = format_forge_one(snap, time.time(), btc_note="BTC в аптренде SMA50")
        self.assertIn("ВХОД", msg)
        self.assertIn("стоп", msg.lower())
        self.assertIn("1x–3x", msg)
        self.assertIn("яма", msg.lower())
        self.assertTrue(snap.buy_ok())

    def test_forge_book_buy_only_when_all_filters(self):
        from scanner import format_forge_book
        bad = ForgeSnapshot(symbol="JUNK", n_bars=120, resid=-0.02, liquid=False)
        good = ForgeSnapshot(
            symbol="SOL", n_bars=120, close=100.0, resid=0.05, vol=0.02,
            sma=90.0, stop=92.0, atr=2.0, above_sma=True, chandelier_ok=True,
            quiet=True, liquid=True, picked=True, entry=True,
        )
        msg = format_forge_book([good, bad], time.time(), scanned=40, universe=40)
        self.assertIn("SOL", msg)
        self.assertIn("КУПИТЬ", msg)
        self.assertIn("7 фильтров", msg)


# ---------------------------------------------------------------------------
# Режим «все монеты» и свежесть данных
# ---------------------------------------------------------------------------

class TestAllCoinsMode(unittest.TestCase):
    def test_resolve_bases_returns_all_supported(self):
        """TOP_SYMBOLS=0 (дефолт): сканируются ВСЕ поддерживаемые монеты."""
        settings = make_settings(top_symbols_limit=0)
        spot = make_side("mexc", "spot", {
            "BTC": make_quote(bid=99, ask=100),
            "PEPE": make_quote(bid=0.0001, ask=0.00011),
            "XYZ": make_quote(bid=1, ask=1),  # есть только на споте — не сканируется
        })
        fut = make_side("bybit", "futures", {
            "BTC": make_quote(bid=103, ask=104),
            "PEPE": make_quote(bid=0.000115, ask=0.000116),
        })
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        bases = asyncio.run(scanner._resolve_bases())
        self.assertEqual(set(bases), {"BTC", "PEPE"})

    def test_resolve_bases_fallback_list_when_no_intersection(self):
        """Нет пересечений вообще — используется резервный список (фильтруется)."""
        settings = make_settings(top_symbols_limit=0)
        spot = make_side("mexc", "spot", {"XYZONLYSPOT": make_quote(bid=1, ask=1)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=2, ask=3)})
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        bases = asyncio.run(scanner._resolve_bases())
        # BTC из резервного списка поддержан фьючерсами Bybit? нет спота → пусто
        # FALLBACK_BASES отфильтруется по факту: спот-сторон с этими базами нет.
        self.assertEqual(bases, [])

    def test_rest_mode_long_round_extends_quote_validity(self):
        """REST-круг по тысячам монет длинный — котировки не «протухают» раньше времени."""
        settings = make_settings(book_max_age_seconds=45.0)
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100, age_seconds=150)})
        spot.mode = "rest"
        spot.last_round_seconds = 120.0  # эффективный возраст = 2*120+10 = 250с
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=110, ask=111)})
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        self.assertGreaterEqual(spot.effective_book_max_age(), 250.0)
        pairs = scanner._evaluate()
        self.assertEqual(len(pairs), 1)  # котировка 150с всё ещё валидна

    def test_ws_mode_uses_plain_book_max_age(self):
        settings = make_settings(book_max_age_seconds=45.0)
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100, age_seconds=60)})
        spot.mode = "ws"
        spot.last_round_seconds = 0.0
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=110, ask=111)})
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        self.assertEqual(spot.effective_book_max_age(), 45.0)
        self.assertEqual(scanner._evaluate(), [])  # 60с > 45с — котировка устарела

    def test_opportunity_carries_data_age(self):
        settings = make_settings()
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100, age_seconds=10)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=110, ask=111)})
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        best, _ = scanner._evaluate()[0]
        self.assertGreaterEqual(best.data_age_seconds, 10.0)
        message = format_signal_message(best, [], settings, cooldown_until=0.0)
        self.assertIn("Возраст котировок", message)


# ---------------------------------------------------------------------------
# v2: режим on_demand — никакого спама, сигналы только по запросу
# ---------------------------------------------------------------------------

class TestOnDemandMode(unittest.TestCase):
    def _scanner_with_signal(self, notifier, **settings_overrides) -> ArbitrageScanner:
        # signal_mode по умолчанию = "on_demand"
        settings = make_settings(**settings_overrides)
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        # 103/104 → gross 3.00%, net 2.85% (выше порога 2.0%)
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103, ask=104)})
        return make_scanner(settings, [spot], [fut], notifier)

    def test_default_mode_is_on_demand(self):
        self.assertEqual(make_settings().signal_mode, "on_demand")

    def test_on_demand_process_signals_never_pushes(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)
        pairs = scanner._evaluate()
        self.assertEqual(len(pairs), 1)  # спред 2.85% выше порога 2.0%
        asyncio.run(scanner._process_signals(pairs))
        self.assertEqual(len(notifier.messages), 0)  # НИЧЕГО не отправлено
        self.assertEqual(scanner.stats["signals_sent"], 0)

    def test_on_demand_records_events_without_pushing(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)

        scanner._record_events(scanner._evaluate())
        self.assertEqual(len(notifier.messages), 0)
        self.assertEqual(scanner.stats["events_recorded"], 1)

        # Дубль по той же паре в пределах кулдауна не логируется повторно
        scanner._record_events(scanner._evaluate())
        self.assertEqual(scanner.stats["events_recorded"], 1)

        # После истечения кулдауна событие фиксируется снова
        scanner._event_cooldown_until["BTC"] = time.time() - 1.0
        scanner._record_events(scanner._evaluate())
        self.assertEqual(scanner.stats["events_recorded"], 2)

    def test_signals_command_shows_event_log(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)
        message = asyncio.run(scanner._cmd_signals("111", ""))
        self.assertIn("Пока пусто", message)

        scanner._record_events(scanner._evaluate())
        message = asyncio.run(scanner._cmd_signals("111", ""))
        for needle in ("BTC", "+2.85%", "MEXC", "Bybit"):
            self.assertIn(needle, message)

    def test_signal_command_returns_full_breakdown_on_request(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)
        message = asyncio.run(scanner._cmd_signal("111", ""))
        for needle in ("BTC/USDT", "+2.85%", "+3.00%", "MEXC", "Bybit",
                       "100.00", "103.00", "ПОШАГОВЫЙ ПЛАН"):
            self.assertIn(needle, message)
        self.assertEqual(notifier.messages, [])  # и снова — никакого push

    def test_signal_command_unknown_base(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)
        message = asyncio.run(scanner._cmd_signal("111", "NOSUCHCOIN"))
        self.assertIn("нет свежих данных", message)

    def test_top_table_columns(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)
        message = asyncio.run(scanner._cmd_top("111", ""))
        for needle in ("МОНЕТА", "ГДЕ КУПИТЬ", "ГДЕ ШОРТ", "ЦЕНА↓", "ЦЕНА↑", "ГРОСС", "NET",
                       "MEXC", "100.0000", "Bybit", "103.0000", "+3.00%", "+2.85%",
                       "+$2.85",  # исполнимый профит со $100 при NET +2.85%
                       "ДЕТАЛЬНЫЙ РАЗБОР", "Исполнимость", "Оценка"):
            self.assertIn(needle, message)
        # полные названия бирж — без коротких тегов
        self.assertIn("MEXC", message)
        # глубины $1.0k хватает на вход $100 — в таблице нет строк «≤$X!»
        table_block = message.split("<pre>")[1].split("</pre>")[0]
        self.assertNotIn("≤$", table_block)
        self.assertNotIn("!", table_block)

    def test_top_low_liquidity_warning_marker(self):
        """Глубины меньше $100: «С $100» показывает максимум входа «≤$X!»."""
        notifier = FakeNotifier()
        settings = make_settings()
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100, ask_qty=0.5)})      # $50
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103, ask=104, bid_qty=0.6)})  # $61.8
        scanner = make_scanner(settings, [spot], [fut], notifier)
        message = asyncio.run(scanner._cmd_top("111", ""))
        # Тонкий стакан — не качество: /top не показывает мусор как арбитраж.
        self.assertIn("нет исполнимых", message)
        self.assertNotIn("MEXC", message)

    def test_top_fs_reverse_direction(self):
        notifier = FakeNotifier()
        scanner = self._scanner_with_signal(notifier)
        message = asyncio.run(scanner._cmd_top("111", "fs"))
        self.assertIn("F→S", message)
        self.assertIn("нет исполнимых", message)
        self.assertNotIn("-4.96%", message)


# ---------------------------------------------------------------------------
# Анти-мусор: «одинаковый тикер ≠ одна монета» + потолок спреда
# ---------------------------------------------------------------------------

class TestPriceSanity(unittest.TestCase):
    """
    Разные биржи листают РАЗНЫЕ монеты под одним тикером (CAT на OKX-споте —
    Simon's Cat за $0.000002, а «CAT»-перп на другой бирже — иной проект за
    $817). Раньше это рождало «спреды» в миллиарды процентов. Теперь:
      1) котировка, дикая относительно МЕДИАНЫ по биржам, в связки не идёт;
      2) гросс-спред выше MAX_SPREAD_PERCENT скрывается как неисполнимый.
    """

    def _cat_collision_sides(self):
        """Реальный кейс пользователя: CAT ~$0.000002 на спотах vs $817 на перпе."""
        spot_okx = make_side("okx", "spot", {"CAT": make_quote(bid=0.00000196, ask=0.00000197)})
        spot_mexc = make_side("mexc", "spot", {"CAT": make_quote(bid=0.00000195, ask=0.00000196)})
        fut_binance = make_side("binance", "futures", {"CAT": make_quote(bid=817.51, ask=817.52)})
        return [spot_okx, spot_mexc], [fut_binance]

    def test_same_ticker_different_token_completely_filtered(self):
        spot_sides, fut_sides = self._cat_collision_sides()
        scanner = make_scanner(make_settings(), spot_sides, fut_sides, FakeNotifier())
        self.assertEqual(scanner._evaluate(), [])  # миллиардных «спредов» больше нет
        by_base = scanner._collect_opportunities(threshold=float("-inf"))
        self.assertNotIn("CAT", by_base)
        self.assertGreater(scanner.stats["suspicious_quotes_skipped"], 0)

    def test_outlier_detection_marks_only_deviant_side(self):
        spot_sides, fut_sides = self._cat_collision_sides()
        scanner = make_scanner(make_settings(), spot_sides, fut_sides, FakeNotifier())
        outliers = scanner._compute_price_outliers()
        # Помечен только фьючерсный Binance (цена в ~415 млрд раз от медианы),
        # споты OKX/MEXC с нормальной ценой — чисты.
        self.assertIn(("binance", "futures", "CAT"), outliers)
        self.assertNotIn(("okx", "spot", "CAT"), outliers)
        self.assertNotIn(("mexc", "spot", "CAT"), outliers)

    def test_two_hugely_disagreeing_sources_drop_base(self):
        """1 спот + 1 перп с ценами в 100 раз apart — оба выбросы, связки нет."""
        spot = make_side("mexc", "spot", {"MA": make_quote(bid=0.99, ask=1.00)})
        fut = make_side("bybit", "futures", {"MA": make_quote(bid=100.0, ask=101.0)})
        scanner = make_scanner(make_settings(), [spot], [fut], FakeNotifier())
        self.assertEqual(scanner._evaluate(), [])

    def test_twenty_percent_spread_is_junk(self):
        """~20% между ногами — не арбитраж."""
        spot = make_side("mexc", "spot", {"XYZ": make_quote(bid=99.0, ask=100.0)})
        fut = make_side("bybit", "futures", {"XYZ": make_quote(bid=120.0, ask=121.0)})
        scanner = make_scanner(
            make_settings(max_spread_percent=8.0, price_deviation_max_percent=15.0),
            [spot], [fut], FakeNotifier(),
        )
        self.assertEqual(scanner._evaluate(), [])
        self.assertNotIn("XYZ", scanner._collect_opportunities(threshold=float("-inf")))

    def test_wide_intra_book_spread_skipped(self):
        spot = make_side("mexc", "spot", {"AAA": make_quote(bid=90.0, ask=100.0)})
        fut = make_side("bybit", "futures", {"AAA": make_quote(bid=103.0, ask=104.0)})
        scanner = make_scanner(
            make_settings(max_book_spread_percent=2.0, max_spread_percent=30.0),
            [spot], [fut], FakeNotifier(),
        )
        self.assertEqual(scanner._collect_opportunities(threshold=float("-inf")), {})
        self.assertGreater(scanner.stats["wide_book_skipped"], 0)

    def test_legit_small_spread_survives_filters(self):
        """Обычный рабочий спред 3% фильтрами не трогается."""
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99.0, ask=100.0)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103.0, ask=104.0)})
        scanner = make_scanner(make_settings(), [spot], [fut], FakeNotifier())
        pairs = scanner._evaluate()
        self.assertEqual(len(pairs), 1)
        self.assertAlmostEqual(pairs[0][0].net_spread_percent, 2.85, places=6)
        self.assertEqual(scanner._compute_price_outliers(), {})

    def test_spread_cap_hides_unexecutable_gaps(self):
        """Согласованные цены, но гросс ~40% — выше потолка 30%, связка скрыта."""
        spot_mexc = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        spot_okx = make_side("okx", "spot", {"BTC": make_quote(bid=100, ask=101)})
        fut_bybit = make_side("bybit", "futures", {"BTC": make_quote(bid=140, ask=141)})
        scanner = make_scanner(
            make_settings(), [spot_mexc, spot_okx], [fut_bybit], FakeNotifier()
        )
        self.assertEqual(scanner._collect_opportunities(threshold=float("-inf")), {})
        self.assertGreater(scanner.stats["spread_capped_skipped"], 0)
        # Потолок выключен (0) — связка возвращается (админ осознанно снял фильтр).
        scanner_off = make_scanner(
            make_settings(max_spread_percent=0.0),
            [spot_mexc, spot_okx], [fut_bybit], FakeNotifier(),
        )
        by_base = scanner_off._collect_opportunities(threshold=float("-inf"))
        self.assertEqual(len(by_base["BTC"]), 2)

    def test_filters_can_be_disabled_for_debugging(self):
        spot_sides, fut_sides = self._cat_collision_sides()
        scanner = make_scanner(
            make_settings(price_deviation_max_percent=0.0, max_spread_percent=0.0),
            spot_sides, fut_sides, FakeNotifier(),
        )
        by_base = scanner._collect_opportunities(threshold=float("-inf"))
        self.assertIn("CAT", by_base)  # без фильтра мусор возвращается

    def test_top_message_hides_fakes_but_shows_counter(self):
        """В /top видны только настоящие связки + счётчик отфильтрованных подделок."""
        spot_real = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        fut_real = make_side("bybit", "futures", {"BTC": make_quote(bid=103, ask=104)})
        spot_fake = make_side("okx", "spot", {"WEN": make_quote(bid=0.00001274, ask=0.00001275)})
        fut_fake = make_side("binance", "futures", {"WEN": make_quote(bid=7.91, ask=7.92)})
        scanner = make_scanner(
            make_settings(), [spot_real, spot_fake], [fut_real, fut_fake], FakeNotifier()
        )
        message = asyncio.run(scanner._cmd_top("111", ""))
        self.assertIn("BTC", message)
        self.assertIn("MEXC", message)
        self.assertIn("100.0000", message)
        self.assertIn("Bybit", message)
        self.assertIn("103.0000", message)
        self.assertNotIn("WEN", message)          # подделка не показана
        self.assertNotIn("817", message)
        self.assertNotIn("7.91", message)
        self.assertIn("Отфильтровано", message)   # но счётчик — виден

    def test_coin_message_marks_suspicious_exchange(self):
        """/coin помечает ⚠ биржу с дикой ценой и показывает медиану."""
        spot_sides, fut_sides = self._cat_collision_sides()
        scanner = make_scanner(make_settings(), spot_sides, fut_sides, FakeNotifier())
        message = asyncio.run(scanner._cmd_price("111", "CAT"))
        self.assertIn("CAT/USDT", message)
        self.assertIn("0.00000197", message)      # настоящая цена OKX
        self.assertIn("817.5100", message)        # цена подделки видна, но…
        self.assertIn("⚠", message)               # …помечена предупреждением
        self.assertIn("Медианная цена", message)  # ориентир для новичка
        self.assertIn("другая монета с тем же тикером", message)

    def test_signal_message_shows_exact_price_gap(self):
        """/signal всегда содержит обе цены и точный разрыв Δ."""
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103, ask=104)})
        scanner = make_scanner(make_settings(), [spot], [fut], FakeNotifier())
        message = asyncio.run(scanner._cmd_signal("111", ""))
        self.assertIn("Разрыв цен", message)
        self.assertIn("100.00", message)
        self.assertIn("103.00", message)
        self.assertIn("Δ = 3.00", message)

    def test_startup_message_mentions_on_demand_mode(self):
        settings = make_settings()
        message = format_startup_message(settings, ["BTC"])
        self.assertIn("ТОЛЬКО ПО ЗАПРОСУ", message)

    def test_startup_message_auto_mode(self):
        settings = make_settings(signal_mode="auto")
        message = format_startup_message(settings, ["BTC"])
        self.assertIn("авто-push", message)


# ---------------------------------------------------------------------------
# Исполнимость спреда: глубина стакана + VWAP на размер входа
# ---------------------------------------------------------------------------

def make_depth_quote(
    ask_levels: list[tuple[float, float]],
    bid_levels: list[tuple[float, float]],
    *,
    age_seconds: float = 0.0,
) -> BookQuote:
    """Котировка с реальной глубиной (несколько уровней в стакане)."""
    bid, bid_qty = bid_levels[0]
    ask, ask_qty = ask_levels[0]
    return BookQuote(
        bid=bid, bid_qty=bid_qty, ask=ask, ask_qty=ask_qty,
        bid_notional_usd=bid * bid_qty, ask_notional_usd=ask * ask_qty,
        timestamp=time.time() - age_seconds,
        ask_levels=tuple(ask_levels),
        bid_levels=tuple(bid_levels),
    )


class TestExecutableSpread(unittest.TestCase):
    """Гросс/NET — топ стакана; исполнимость — что будет на входе $100."""

    def setUp(self):
        self.settings = make_settings()  # eval_notional_usd = 100

    def test_vwap_walk_math(self):
        """Набор $100 по двум уровням: $50 по 100 + $50 по 101 → VWAP ≈100.4975."""
        vwap, filled = _walk_book_vwap(((100.0, 0.5), (101.0, 0.5)), 100.0)
        self.assertAlmostEqual(filled, 100.0)
        # база: 0.5 монеты по 100 + 50/101 монеты по 101 = 0.99505
        self.assertAlmostEqual(vwap, 100.0 / (0.5 + 50.0 / 101.0), places=6)
        # Частичный набор: только $75 → $50 по 100 + $25 по 101
        vwap, filled = _walk_book_vwap(((100.0, 0.5), (101.0, 0.5)), 75.0)
        self.assertAlmostEqual(filled, 75.0)
        self.assertAlmostEqual(vwap, 75.0 / (0.5 + 25.0 / 101.0), places=6)
        # Глубины не хватило: просили $200, есть лишь $100.5
        vwap, filled = _walk_book_vwap(((100.0, 0.5), (101.0, 0.5)), 200.0)
        self.assertAlmostEqual(filled, 100.5)
        self.assertAlmostEqual(vwap, 100.5 / 1.0, places=6)

    def test_store_quote_rejects_crossed_book(self):
        side = make_side("mexc", "spot", {})
        side._store_quote("BTC/USDT", {
            "bids": [[101.0, 1.0]],
            "asks": [[100.0, 1.0]],
        })
        self.assertNotIn("BTC/USDT", side.quotes)

    def test_store_quote_keeps_depth_levels(self):
        """_store_quote разбирает все уровни книги и сортирует их."""
        side = make_side("mexc", "spot", {})
        side._store_quote("BTC/USDT", {
            "bids": [[99.0, 2.0], [98.0, 3.0]],
            "asks": [[100.0, 1.0], [101.0, 4.0], [102.0, 5.0]],
        })
        quote = side.quotes["BTC/USDT"]
        self.assertEqual(quote.ask_levels, ((100.0, 1.0), (101.0, 4.0), (102.0, 5.0)))
        self.assertEqual(quote.bid_levels, ((99.0, 2.0), (98.0, 3.0)))
        self.assertAlmostEqual(quote.ask_depth_usd, 100.0 + 404.0 + 510.0)
        self.assertAlmostEqual(quote.bid_depth_usd, 198.0 + 294.0)
        vwap, filled = quote.vwap_ask(504.0)  # $100 + $404 по первым двум уровням
        self.assertAlmostEqual(filled, 504.0)
        self.assertAlmostEqual(vwap, 504.0 / 5.0, places=6)

    def test_single_level_quote_falls_back_to_top_of_book(self):
        """Котировка без уровней (старый формат) деградирует до лучшей цены."""
        quote = make_quote(bid=99.0, ask=100.0, bid_qty=2.0, ask_qty=3.0)
        self.assertEqual(quote.ask_levels, ((100.0, 3.0),))
        self.assertEqual(quote.bid_levels, ((99.0, 2.0),))
        self.assertAlmostEqual(quote.ask_depth_usd, 300.0)
        vwap, filled = quote.vwap_ask(150.0)
        self.assertAlmostEqual(filled, 150.0)
        self.assertAlmostEqual(vwap, 100.0)

    def test_exec_fields_on_deep_book(self):
        """Глубокий стакан: $100 входит, VWAP-спред чуть меньше топового."""
        # спот: ask 100×0.6=$60 + 100.5×0.5=$50.25 → $110.25 глубины
        spot = make_side("mexc", "spot", {"BTC": make_depth_quote(
            ask_levels=[(100.0, 0.6), (100.5, 0.5)],
            bid_levels=[(99.0, 1.0)],
        )})
        # перп: bid 103×0.6=$61.8 + 102.5×0.5=$51.25 → $113.05 глубины
        fut = make_side("bybit", "futures", {"BTC": make_depth_quote(
            ask_levels=[(104.0, 1.0)],
            bid_levels=[(103.0, 0.6), (102.5, 0.5)],
        )})
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())
        by_base = scanner._collect_opportunities(threshold=float("-inf"))
        opp = by_base["BTC"][0]
        # Топ стакана: (103 − 100)/100 = +3.00% − 0.15% = +2.85%
        self.assertAlmostEqual(opp.net_spread_percent, 2.85, places=6)
        # На $100 обе ноги вместились целиком
        self.assertTrue(opp.exec_fully_filled)
        self.assertAlmostEqual(opp.exec_size_usd, 100.0)
        self.assertAlmostEqual(opp.fillable_usd, min(110.25, 113.05), places=6)
        # VWAP-покупка: $60 по 100 + $40 по 100.5 → 100.1996 (проскальзывание)
        self.assertAlmostEqual(opp.exec_buy_price, 100.0 / (0.6 + 40.0 / 100.5), places=6)
        self.assertAlmostEqual(
            opp.exec_sell_price, 100.0 / (0.6 + 38.2 / 102.5), places=6
        )
        # Исполнимый спред меньше топового (проскальзывание по уровням)
        self.assertLess(opp.exec_net_spread_percent, opp.net_spread_percent)

    def test_exec_fields_on_thin_book(self):
        """Тонкий стакан: на $100 не войти, exec_size = видимой глубине."""
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99.0, ask=100.0, ask_qty=0.5)})   # $50
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103.0, ask=104.0, bid_qty=0.6)})  # $61.8
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())
        by_base = scanner._collect_opportunities(threshold=float("-inf"))
        opp = by_base["BTC"][0]
        self.assertFalse(opp.exec_fully_filled)
        self.assertAlmostEqual(opp.exec_size_usd, 50.0)
        self.assertAlmostEqual(opp.fillable_usd, 50.0)
        # VWAP на $50 — это лучшая цена (единственный уровень)
        self.assertAlmostEqual(opp.exec_buy_price, 100.0)
        self.assertAlmostEqual(opp.exec_sell_price, 103.0)
        self.assertAlmostEqual(opp.exec_net_spread_percent, 2.85, places=6)

    def test_top_marks_thin_rows_and_shows_exec_profit(self):
        """В /top тонкие строки помечены «≤$X!», глубокие — профитом."""
        spot_thin = make_side("mexc", "spot", {"BP": make_quote(bid=0.437, ask=0.438, ask_qty=2.0)})   # $0.876
        fut_thin = make_side("bybit", "futures", {"BP": make_quote(bid=0.450, ask=0.451, bid_qty=2.0)})
        spot_deep = make_side("okx", "spot", {"HIM": make_quote(bid=0.00872, ask=0.00873, ask_qty=20000.0)})  # $174
        fut_deep = make_side("binance", "futures", {"HIM": make_quote(bid=0.00882, ask=0.00883, bid_qty=20000.0)})
        scanner = make_scanner(
            self.settings, [spot_thin, spot_deep], [fut_thin, fut_deep], FakeNotifier()
        )
        message = asyncio.run(scanner._cmd_top("111", ""))
        table_block = message.split("<pre>")[1].split("</pre>")[0]
        self.assertIn("HIM", table_block)
        self.assertIn("Binance", table_block)
        self.assertIn("OKX", table_block)
        self.assertNotIn("BP", table_block)
        self.assertNotIn("≤$0.88!", table_block)

    def test_signal_message_contains_exec_line(self):
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99.0, ask=100.0)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103.0, ask=104.0)})
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())
        message = asyncio.run(scanner._cmd_signal("111", ""))
        self.assertIn("Исполнимость", message)
        self.assertIn("вмещается в стакан", message)

    def test_calc_warns_when_amount_exceeds_depth(self):
        """/calc на сумму больше видимой глубины предупреждает."""
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99.0, ask=100.0, ask_qty=1.0)})   # $100
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103.0, ask=104.0, bid_qty=1.0)})
        scanner = make_scanner(self.settings, [spot], [fut], FakeNotifier())
        message = asyncio.run(scanner._cmd_calc("111", "BTC 5000"))
        self.assertIn("больше видимой глубины", message)
        self.assertIn("проскальзывание", message)
        # На сумму, которая влезает, — расчёт VWAP присутствует
        message = asyncio.run(scanner._cmd_calc("111", "BTC 50"))
        self.assertIn("С учётом стакана (VWAP)", message)

    def test_eval_notional_env_parsing(self):
        with mock.patch.dict(os.environ, {"EVAL_NOTIONAL_USD": "250"}, clear=True):
            settings = Settings.from_env()
            self.assertAlmostEqual(settings.eval_notional_usd, 250.0)
        with mock.patch.dict(os.environ, {"EVAL_NOTIONAL_USD": "0"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()
        # описания конфигурации упоминают размер проверки
        self.assertIn("$100", make_settings().describe())


# ---------------------------------------------------------------------------
# Funding-рейты (по запросу, с кэшем)
# ---------------------------------------------------------------------------

class FundingStubExchange:
    """Заглушка фьючерсной биржи с funding-рейтом (offline-тест)."""

    def __init__(self, rate: float = 0.0001):
        self.rate = rate
        self.markets = {"BTC/USDT:USDT": {"type": "swap", "active": True}}
        self.calls = 0

    async def fetch_funding_rate(self, symbol, params=None):
        self.calls += 1
        return {"fundingRate": self.rate, "symbol": symbol}

    async def close(self):
        pass


class TestFundingRates(unittest.TestCase):
    def _scanner_with_funding(self, rate: float = 0.0001) -> ArbitrageScanner:
        settings = make_settings()
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103, ask=104)})
        fut.exchange = FundingStubExchange(rate)
        return make_scanner(settings, [spot], [fut], FakeNotifier())

    def test_funding_command_table(self):
        scanner = self._scanner_with_funding()
        message = asyncio.run(scanner._cmd_funding("111", "BTC"))
        self.assertIn("Bybit", message)
        self.assertIn("+0.0100%", message)
        self.assertIn("лонги → шортам", message)

    def test_funding_apr_math(self):
        # 0.01% за 8ч = 0.03%/день ≈ +10.95% APR
        scanner = self._scanner_with_funding()
        message = asyncio.run(scanner._cmd_funding("111", "BTC"))
        self.assertIn("+10.95%", message)

    def test_funding_cache_prevents_refetch(self):
        scanner = self._scanner_with_funding()
        asyncio.run(scanner._get_funding_map("BTC"))
        calls_first = scanner.futures_sides[0].exchange.calls
        self.assertEqual(calls_first, 1)
        asyncio.run(scanner._get_funding_map("BTC"))
        self.assertEqual(scanner.futures_sides[0].exchange.calls, calls_first)  # из кэша

    def test_price_message_contains_funding(self):
        scanner = self._scanner_with_funding()
        message = asyncio.run(scanner._cmd_price("111", "BTC"))
        self.assertIn("+0.0100%", message)

    def test_signal_message_contains_funding(self):
        scanner = self._scanner_with_funding()
        message = asyncio.run(scanner._cmd_signal("111", "BTC"))
        self.assertIn("Funding Bybit", message)
        self.assertIn("+0.0100%", message)

    def test_funding_disabled(self):
        settings = make_settings(funding_enabled=False)
        spot = make_side("mexc", "spot", {"BTC": make_quote(bid=99, ask=100)})
        fut = make_side("bybit", "futures", {"BTC": make_quote(bid=103, ask=104)})
        scanner = make_scanner(settings, [spot], [fut], FakeNotifier())
        message = asyncio.run(scanner._cmd_funding("111", "BTC"))
        self.assertIn("отключён", message)


# ---------------------------------------------------------------------------
# Список монет /coins и разбор /coin (столбики, обе стороны)
# ---------------------------------------------------------------------------

class TestCoinsAndBreakdown(unittest.TestCase):
    def _scanner_two_coins(self) -> ArbitrageScanner:
        settings = make_settings()
        spot = make_side("mexc", "spot", {
            "BTC": make_quote(bid=99, ask=100),
            "ETH": make_quote(bid=2000, ask=2005),
        })
        fut = make_side("bybit", "futures", {
            "BTC": make_quote(bid=103, ask=104),
            "ETH": make_quote(bid=2010, ask=2015),
        })
        return make_scanner(settings, [spot], [fut], FakeNotifier())

    def test_coins_command_lists_bases(self):
        scanner = self._scanner_two_coins()
        message = asyncio.run(scanner._cmd_coins("111", ""))
        self.assertIn("МОНЕТЫ", message)
        self.assertIn("Сканируется 2 пар", message)
        self.assertIn("BTC", message)
        self.assertIn("ETH", message)
        self.assertIn("ОБЪЁМ 24Ч", message)

    def test_coins_spread_column(self):
        scanner = self._scanner_two_coins()
        message = asyncio.run(scanner._cmd_coins("111", ""))
        # BTC: (103−100)/100 − 0.15 = +2.85%; ETH: (2010−2005)/2005 − 0.15 ≈ +0.10%
        self.assertIn("+2.85%", message)
        self.assertIn("+0.10%", message)

    def test_price_breakdown_has_all_sections(self):
        scanner = self._scanner_two_coins()
        message = asyncio.run(scanner._cmd_price("111", "eth"))
        for needle in ("СПОТ USDT", "ПЕРПЕТУАЛЫ USDT", "ЛУЧШАЯ СВЯЗКА",
                       "ПЛАН ДЕЙСТВИЙ", "Обратное направление"):
            self.assertIn(needle, message)
        self.assertIn("2,005.00", message)  # реальный ask ETH на MEXC

    def test_opportunity_direction_properties(self):
        scanner = self._scanner_two_coins()
        by_base = scanner._collect_opportunities(threshold=float("-inf"))
        opp = by_base["BTC"][0]
        self.assertEqual(opp.direction, DIR_SPOT_TO_FUT)
        self.assertEqual(opp.buy_exchange, "MEXC")
        self.assertEqual(opp.buy_price, 100.0)
        self.assertEqual(opp.sell_exchange, "Bybit")
        self.assertEqual(opp.sell_price, 103.0)
        by_base_f = scanner._collect_opportunities(
            threshold=float("-inf"), direction=DIR_FUT_TO_SPOT
        )
        opp_f = by_base_f["BTC"][0]
        self.assertEqual(opp_f.direction, DIR_FUT_TO_SPOT)
        self.assertEqual(opp_f.buy_exchange, "Bybit")
        self.assertEqual(opp_f.buy_price, 104.0)
        self.assertEqual(opp_f.sell_exchange, "MEXC")
        self.assertEqual(opp_f.sell_price, 99.0)


# ---------------------------------------------------------------------------
# Полный жизненный цикл (startup → фоновый цикл → shutdown) со стабами бирж
# ---------------------------------------------------------------------------

class StubRestExchange:
    """Стаб REST-биржи: load_markets + fetch_order_book, без сети."""

    def __init__(self, spot_book=(99.0, 100.0), fut_book=(103.0, 104.0)):
        self.has = {"watchOrderBook": False}
        self.markets = {}
        self._spot_book = spot_book
        self._fut_book = fut_book
        self.closed = False

    async def load_markets(self, *args, **kwargs):
        self.markets = {
            "BTC/USDT": {"active": True, "quote": "USDT", "base": "BTC", "type": "spot"},
            "BTC/USDT:USDT": {
                "active": True, "quote": "USDT", "base": "BTC",
                "type": "swap", "swap": True, "linear": True, "settle": "USDT",
            },
        }
        return self.markets

    async def fetch_order_book(self, symbol, limit=None):
        book = self._spot_book if symbol.endswith("/USDT") else self._fut_book
        bid, ask = book
        return {"bids": [[bid, 10.0]], "asks": [[ask, 10.0]], "timestamp": 0}

    async def close(self):
        self.closed = True


class ScenarioScanner(ArbitrageScanner):
    """Сканер с заранее созданными сторонами-стабами (без реального ccxt)."""

    def __init__(self, settings: Settings, notifier, sides):
        super().__init__(settings, notifier)
        self._sides = sides

    def _build_sides(self) -> None:
        for side in self._sides:
            target = self.spot_sides if side.market_type == "spot" else self.futures_sides
            target.append(side)


class TestFullLifecycle(unittest.TestCase):
    def _make(self, **overrides):
        settings = make_settings(
            scan_interval_seconds=0.2,
            rest_poll_interval_seconds=0.05,
            rest_throttle_seconds=0.0,
            **overrides,
        )
        spot = ExchangeSide(settings, EXCHANGE_REGISTRY["mexc"], "spot",
                            exchange=StubRestExchange())
        fut = ExchangeSide(settings, EXCHANGE_REGISTRY["bybit"], "futures",
                           exchange=StubRestExchange())
        notifier = FakeNotifier()
        scanner = ScenarioScanner(settings, notifier, [spot, fut])
        return notifier, scanner

    def test_on_demand_lifecycle_no_spam(self):
        """on_demand: ровно одно стартовое сообщение, push-сигналов НЕТ."""
        notifier, scanner = self._make()

        async def scenario():
            task = asyncio.create_task(scanner.run())
            await asyncio.sleep(1.2)  # старт + несколько циклов сканирования
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("ТОЛЬКО ПО ЗАПРОСУ", notifier.messages[0])
        self.assertEqual(scanner.stats["signals_sent"], 0)
        # но события ≥ порога в журнале есть
        self.assertGreaterEqual(scanner.stats["events_recorded"], 1)
        message = asyncio.run(scanner._cmd_signals("111", ""))
        self.assertIn("BTC", message)

    def test_auto_lifecycle_pushes_once_then_cooldown(self):
        """auto: startup + один сигнал, дальше кулдаун глушит дубли."""
        notifier, scanner = self._make(signal_mode="auto", cooldown_minutes=10.0)

        async def scenario():
            task = asyncio.create_task(scanner.run())
            await asyncio.sleep(1.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        self.assertEqual(len(notifier.messages), 2)  # startup + 1 сигнал
        self.assertIn("АРБИТРАЖНАЯ СВЯЗКА", notifier.messages[1])
        self.assertIn("не раньше", notifier.messages[1])
        self.assertEqual(scanner.stats["signals_sent"], 1)
        self.assertGreater(scanner.stats["signals_suppressed_cooldown"], 0)

    def test_startup_message_disabled(self):
        notifier, scanner = self._make(startup_message=False)

        async def scenario():
            task = asyncio.create_task(scanner.run())
            await asyncio.sleep(0.7)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        self.assertEqual(notifier.messages, [])  # вообще ничего не прислал


# ---------------------------------------------------------------------------
# Конфиг: новый режим сигналов
# ---------------------------------------------------------------------------

class TestSignalModeConfig(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.signal_mode, "on_demand")
        self.assertTrue(settings.startup_message)
        self.assertTrue(settings.funding_enabled)

    def test_signal_mode_env(self):
        with mock.patch.dict(os.environ, {"SIGNAL_MODE": "auto"}, clear=True):
            self.assertEqual(Settings.from_env().signal_mode, "auto")
        with mock.patch.dict(os.environ, {"SIGNAL_MODE": "banana"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
