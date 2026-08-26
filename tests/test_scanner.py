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
from scanner import (                                              # noqa: E402
    EXCHANGE_REGISTRY,
    ArbitrageScanner,
    BookQuote,
    ExchangeSide,
    Opportunity,
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
        fut_gate = make_side("gate", "futures", {"BTC": make_quote(bid=102.5, ask=103.5)})
        scanner = make_scanner(self.settings, [spot_mexc, spot_okx], [fut_bybit, fut_gate],
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
    def _scanner_with_signal(self, notifier, **settings_overrides):
        settings = make_settings(**settings_overrides)
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
        settings = make_settings(max_signals_per_scan=1)
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
        self.assertEqual(settings.exchanges, ("mexc", "bybit", "gate", "okx", "binance"))
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
        for command in ("/top", "/status", "/price", "/help"):
            self.assertIn(command, message)

    def test_handlers_registry(self):
        scanner = self._scanner()
        handlers = scanner.telegram_handlers()
        for name in ("start", "help", "status", "top", "spreads", "price"):
            self.assertIn(name, handlers)


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


if __name__ == "__main__":
    unittest.main()
