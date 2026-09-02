"""
tests/test_telegram_handlers.py — транспортный слой: команды, кнопки, ошибки.

Сеть и биржи здесь подменены: проверяем, что на каждое действие пользователя
бот отвечает по-русски, не падает и не теряет состояние (настройки, список).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.config.settings import Settings
from app.data.synthetic import make_snapshot
from app.domain.models import (Direction, MarketContext, ScanReport, Signal,
                               Timeframe)
from app.presentation import keyboards as kb
from app.services.journal import SignalJournal
from app.services.news import NewsService
from app.services.watchlist import Store
from app.signals.engine import SignalEngine
from app.telegram import handlers
from app.telegram.services import BotServices

CHAT_ID = 777

# Единый журнал всех ответов бота (отправка и правка сообщений кнопок).
LOG: List[Dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Подделки Telegram-объектов
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text: str = "", message_id: int = 1) -> None:
        self.text = text
        self.message_id = message_id
        self.edits: List[Dict[str, Any]] = []

    async def edit_text(self, text: str, **kwargs) -> "FakeMessage":
        self.text = text
        self.edits.append({"text": text, **kwargs})
        LOG.append({"via": "message.edit_text", "text": text, **kwargs})
        return self


class FakeQuery:
    def __init__(self, data: str, message_id: int = 5) -> None:
        self.data = data
        self.message = FakeMessage("старое сообщение", message_id)
        self.answered = False

    async def answer(self, *args, **kwargs) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.message.text = text
        self.message.edits.append({"text": text, **kwargs})
        LOG.append({"via": "query.edit", "text": text, **kwargs})


class FakeChat:
    def __init__(self, chat_id: int = CHAT_ID) -> None:
        self.id = chat_id


class FakeUpdate:
    def __init__(self, *, query: Optional[FakeQuery] = None,
                 text: Optional[str] = None) -> None:
        self.callback_query = query
        self.effective_chat = FakeChat()
        self.message = FakeMessage(text or "") if text is not None else None
        self.effective_message = self.message


class FakeBot:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.edited: List[Dict[str, Any]] = []
        self.chat_actions: List[int] = []
        self._counter = 100

    async def send_message(self, chat_id, text, **kwargs) -> FakeMessage:
        self._counter += 1
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        LOG.append({"via": "send", "chat_id": chat_id, "text": text, **kwargs})
        return FakeMessage(text, self._counter)

    async def edit_message_text(self, chat_id, message_id, text, **kwargs) -> None:
        self.edited.append({"chat_id": chat_id, "message_id": message_id,
                            "text": text, **kwargs})
        LOG.append({"via": "bot.edit", "chat_id": chat_id, "text": text, **kwargs})

    async def send_chat_action(self, chat_id, action) -> None:
        self.chat_actions.append(chat_id)


class FakeApplication:
    def __init__(self) -> None:
        self.bot_data: Dict[str, Any] = {}
        self.pending: List[Any] = []
        self.handlers: List[Any] = []
        self.error_handlers: List[Any] = []

    def create_task(self, coro) -> None:
        self.pending.append(coro)

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    def add_error_handler(self, handler) -> None:
        self.error_handlers.append(handler)


class FakeContext:
    def __init__(self, application, bot, *, query=None, args=None, error=None) -> None:
        self.application = application
        self.bot = bot
        self.callback_query = query
        self.effective_chat = FakeChat()
        self.chat_data: Dict[str, Any] = {}
        self.args = args
        self.error = error


# ---------------------------------------------------------------------------
# Подделки сервисов
# ---------------------------------------------------------------------------

class FakeCache:
    def stats(self):
        return {"size": 0, "hits": 0, "misses": 0, "hit_rate": 0.0}


class FakeMarket:
    """Отдаёт синтетический снимок и «здоровье» без сети."""

    def __init__(self, regime: str = "breakout", seed: int = 21) -> None:
        self.regime = regime
        self.seed = seed
        self.closed = False
        self.candles_cache = FakeCache()

    async def snapshot(self, symbol: str, settings=None):
        return make_snapshot(symbol, self.regime, seed=self.seed, bars=520)

    async def candles(self, symbol, timeframe, limit=300):
        return make_snapshot(symbol, self.regime, seed=self.seed,
                             bars=300).candles[Timeframe.H1]

    def health(self):
        return ["synthetic: офлайн-режим тестов"]

    async def close(self) -> None:
        self.closed = True


class FakeScanner:
    def __init__(self, engine, market, report: Optional[ScanReport] = None) -> None:
        self._engine = engine
        self._market = market
        self._report = report
        self.scans = 0

    @property
    def last_report(self):
        return self._report

    @property
    def is_scanning(self) -> bool:
        return False

    async def run_scan(self, progress=None) -> ScanReport:
        self.scans += 1
        if progress:
            progress("Тестирую", 0.5)
        return self._report

    async def analyze_symbol(self, symbol: str) -> Signal:
        snapshot = await self._market.snapshot(symbol)
        return self._engine.analyze(snapshot, MarketContext())

    def stats(self) -> Dict[str, object]:
        return {"universe": 10, "scanned": 3, "actionable": 1, "longs": 1,
                "shorts": 0, "avg_confidence": 6.5, "duration_s": 1.0,
                "finished_at": "—"}


def _signal(engine) -> Signal:
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    signal = engine.analyze(snapshot, MarketContext(btc_score=0.3, btc_trend="восходящий"))
    assert signal.actionable
    return signal


@pytest.fixture
def env(tmp_path):
    LOG.clear()
    settings = Settings()
    settings.min_confidence = 4.0
    settings.min_rr = 1.1
    settings.min_quote_volume_usd = 100_000
    settings.data_dir = tmp_path

    engine = SignalEngine(settings)
    market = FakeMarket()
    report = ScanReport(signals=[_signal(engine)], scanned=30, universe_size=120)
    services = BotServices(
        settings=settings, market=market, engine=engine,
        scanner=FakeScanner(engine, market, report),
        journal=SignalJournal(tmp_path / "journal.json"),
        store=Store(tmp_path / "store.json"),
        news=NewsService(api_key="", enabled=False),
    )
    application = FakeApplication()
    application.bot_data["services"] = services
    bot = FakeBot()
    return SimpleNamespace(settings=settings, services=services, application=application,
                           bot=bot, report=report, engine=engine)


def context(env, *, query=None, args=None, error=None) -> FakeContext:
    return FakeContext(env.application, env.bot, query=query, args=args, error=error)


async def drain(env) -> None:
    """Дожидается фоновых задач, созданных через application.create_task."""
    while env.application.pending:
        tasks, env.application.pending = env.application.pending, []
        await asyncio.gather(*tasks)


def run(coro):
    return asyncio.run(coro)


def last_text(env) -> str:
    assert LOG, "бот ничего не ответил"
    return LOG[-1]["text"]


def any_text(fragment: str) -> bool:
    return any(fragment in entry["text"] for entry in LOG)


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def test_start_sends_menu_with_buttons(env):
    run(handlers.cmd_start(FakeUpdate(), context(env)))
    call = env.bot.sent[-1]
    assert call["chat_id"] == CHAT_ID
    assert "Найти перспективные монеты" in call["text"]
    assert call["reply_markup"] is not None
    assert "Помощь" in call["text"]


def test_help_explains_how_to_read_signals(env):
    run(handlers.cmd_help(FakeUpdate(), context(env)))
    text = env.bot.sent[-1]["text"]
    assert "Как читать сигнал" in text
    assert "Не является" in text


def test_stats_shows_journal_and_health(env):
    run(handlers.cmd_stats(FakeUpdate(), context(env)))
    text = env.bot.sent[-1]["text"]
    assert "Статистика" in text


def test_health_lists_exchanges(env):
    run(handlers.cmd_health(FakeUpdate(), context(env)))
    assert "synthetic" in env.bot.sent[-1]["text"]


def test_settings_command_renders_current_profile(env):
    run(handlers.cmd_settings(FakeUpdate(), context(env)))
    text = env.bot.sent[-1]["text"]
    assert "Настройки" in text
    assert "Риск-профиль" in text


def test_coin_without_args_asks_for_ticker(env):
    ctx = context(env)
    run(handlers.cmd_coin(FakeUpdate(), ctx))
    assert ctx.chat_data[handlers.PENDING_COIN_KEY] is True
    assert "тик" in env.bot.sent[-1]["text"].lower()


def test_coin_with_args_analyzes(env):
    ctx = context(env, args=["TEST"])
    run(handlers.cmd_coin(FakeUpdate(), ctx))
    run(drain(env))
    assert "$TEST" in last_text(env)


def test_on_text_without_pending_redirects_to_menu(env):
    ctx = context(env)
    run(handlers.on_text(FakeUpdate(text="BTC"), ctx))
    assert "меню" in env.bot.sent[-1]["text"].lower()


def test_on_text_with_pending_runs_analysis(env):
    ctx = context(env)
    ctx.chat_data[handlers.PENDING_COIN_KEY] = True
    run(handlers.on_text(FakeUpdate(text="TEST"), ctx))
    assert handlers.PENDING_COIN_KEY not in ctx.chat_data
    assert "$TEST" in last_text(env)


def test_watch_command_adds_symbol(env):
    run(handlers.cmd_watch(FakeUpdate(), context(env, args=["SOL"])))
    assert env.services.store.watchlist(CHAT_ID) == ["SOL/USDT"]


def test_watch_command_without_args_shows_list(env):
    run(handlers.cmd_watch(FakeUpdate(), context(env)))
    assert "Список" in env.bot.sent[-1]["text"] or "список" in env.bot.sent[-1]["text"]


def test_cancel_clears_pending_state(env):
    ctx = context(env)
    ctx.chat_data[handlers.PENDING_COIN_KEY] = True
    run(handlers.cmd_cancel(FakeUpdate(), ctx))
    assert handlers.PENDING_COIN_KEY not in ctx.chat_data


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------

def test_menu_button_renders_menu(env):
    query = FakeQuery(kb.CB_MENU)
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert query.answered
    assert "Найти перспективные монеты" in last_text(env)


def test_top_button_renders_signals(env):
    query = FakeQuery(kb.CB_TOP)
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert "$TEST" in last_text(env)


def test_market_button_renders_scanner_table(env):
    query = FakeQuery(kb.CB_MARKET)
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert "Сканер рынка" in last_text(env)


def test_pick_button_asks_for_coin(env):
    query = FakeQuery("pick")
    ctx = context(env, query=query)
    run(handlers.on_callback(FakeUpdate(query=query), ctx))
    assert ctx.chat_data[handlers.PENDING_COIN_KEY] is True


def test_signal_button_renders_card(env):
    query = FakeQuery(kb.cb_signal(0))
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    text = last_text(env)
    assert "LONG" in text
    assert "TP1" in text


def test_deep_button_renders_full_analysis(env):
    query = FakeQuery(kb.cb_deep(0))
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert "Глубокий анализ" in last_text(env)


def test_signal_button_out_of_range_is_polite(env):
    query = FakeQuery(kb.cb_signal(9))
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert "устарел" in last_text(env)


def test_coin_button_analyzes(env):
    query = FakeQuery(kb.cb_coin("TEST"))
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert "$TEST" in last_text(env)


def test_bad_ticker_is_explained(env):
    query = FakeQuery(kb.cb_coin("1"))
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert "Не понял тикер" in last_text(env)


def test_watchlist_buttons(env):
    add = FakeQuery(kb.cb_watch_add("BTC"))
    run(handlers.on_callback(FakeUpdate(query=add), context(env, query=add)))
    assert env.services.store.watchlist(CHAT_ID) == ["BTC/USDT"]

    remove = FakeQuery(kb.cb_watch_del("BTC"))
    run(handlers.on_callback(FakeUpdate(query=remove), context(env, query=remove)))
    assert env.services.store.watchlist(CHAT_ID) == []


def test_watchlist_clear_button(env):
    run(env.services.store.watch_add(CHAT_ID, "ETH/USDT"))
    query = FakeQuery(kb.cb_watch_clear())
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert env.services.store.watchlist(CHAT_ID) == []


def test_settings_buttons_change_state(env):
    ctx = context(env)
    query = FakeQuery(kb.cb_set("dep", "5000"))
    run(handlers.on_callback(FakeUpdate(query=query), ctx))
    assert env.services.store.settings(CHAT_ID).deposit_usd == 5000.0

    query = FakeQuery(kb.cb_set("risk", "aggressive"))
    run(handlers.on_callback(FakeUpdate(query=query), ctx))
    stored = env.services.store.settings(CHAT_ID)
    assert stored.risk_profile == "aggressive"
    assert stored.min_confidence == 4.5

    query = FakeQuery(kb.cb_set("push", "0"))
    run(handlers.on_callback(FakeUpdate(query=query), ctx))
    assert env.services.store.settings(CHAT_ID).auto_push is False


def test_settings_button_with_garbage_is_ignored(env):
    query = FakeQuery("set:unknown:42")
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    # Неизвестный ключ — тихий no-op: ничего не меняем и не шумим.
    assert query.answered
    assert LOG == []
    assert env.services.store.settings(CHAT_ID).deposit_usd == 1000.0


def test_unknown_callback_falls_back_to_menu(env):
    query = FakeQuery("какая-то ерунда")
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert "Найти перспективные монеты" in last_text(env)


def test_empty_callback_is_ignored(env):
    query = FakeQuery("")
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert env.bot.sent == [] and env.bot.edited == []


def test_handler_crash_is_reported_not_raised(env, monkeypatch):
    """Поломка сервиса не должна ронять обработчик: показываем меню."""
    def boom():
        raise RuntimeError("всё сломалось")

    monkeypatch.setattr(env.services.journal, "stats", boom)
    query = FakeQuery(kb.CB_STATS)
    run(handlers.on_callback(FakeUpdate(query=query), context(env, query=query)))
    assert "Что-то пошло не так" in last_text(env)


# ---------------------------------------------------------------------------
# Сканирование
# ---------------------------------------------------------------------------

def test_scan_command_publishes_report(env):
    ctx = context(env)
    run(handlers.cmd_scan(FakeUpdate(), ctx))
    run(drain(env))
    assert env.services.scanner.scans == 1
    assert any_text("Топ сигналы")
    assert any_text("$TEST")
    # Прогресс не должен перетирать финальный результат.
    assert "Тестирую" not in last_text(env)
    # Сигнал записан в журнал — чтобы потом честно посчитать точность.
    assert len(env.services.journal.entries()) == 1


def test_scan_marks_typing_action(env):
    run(handlers.cmd_scan(FakeUpdate(), context(env)))
    run(drain(env))
    assert CHAT_ID in env.bot.chat_actions


def test_scan_reports_when_already_running(env, monkeypatch):
    monkeypatch.setattr(type(env.services.scanner), "is_scanning",
                        property(lambda self: True))
    run(handlers.cmd_scan(FakeUpdate(), context(env)))
    run(drain(env))
    assert "уже идёт" in last_text(env)
    assert env.services.scanner.scans == 0


def test_scan_result_pushed_to_subscribers(env):
    """Авто-рассылка идёт только в другие чаты и только для сильных сигналов."""
    env.settings.auto_push = True
    env.settings.push_min_confidence = 1.0
    other_chat = 999

    env.services.store.subscribed_chats = lambda: [other_chat]
    run(handlers.cmd_scan(FakeUpdate(), context(env)))
    run(drain(env))
    assert any(call["chat_id"] == other_chat for call in env.bot.sent)
    assert not any(call["chat_id"] == CHAT_ID and "LONG сигнал" in call["text"]
                   for call in env.bot.sent[1:])


def test_push_disabled_sends_nothing(env):
    env.settings.auto_push = False
    other_chat = 999

    env.services.store.subscribed_chats = lambda: [other_chat]
    run(handlers.cmd_scan(FakeUpdate(), context(env)))
    run(drain(env))
    assert not any(call["chat_id"] == other_chat for call in env.bot.sent)


def test_scan_without_signals_says_so(env, monkeypatch):
    monkeypatch.setattr(env.services.scanner, "_report",
                        ScanReport(signals=[], scanned=10, universe_size=50))
    run(handlers.cmd_scan(FakeUpdate(), context(env)))
    run(drain(env))
    assert any_text("сигналов нет") or any_text("Нет сигналов") or any_text("нет setups")


# ---------------------------------------------------------------------------
# Фоновые задачи и ошибки
# ---------------------------------------------------------------------------

def test_periodic_scan_updates_journal_and_pushes(env):
    run(handlers.periodic_scan(context(env)))
    run(drain(env))
    assert env.services.scanner.scans == 1


def test_periodic_scan_survives_scanner_crash(env, monkeypatch):
    async def boom(progress=None):
        raise RuntimeError("биржа недоступна")

    monkeypatch.setattr(env.services.scanner, "run_scan", boom)
    run(handlers.periodic_scan(context(env)))      # не должно бросить исключение


def test_outcome_check_survives_market_crash(env, monkeypatch):
    async def boom(symbol, timeframe, limit=300):
        raise RuntimeError("нет данных")

    monkeypatch.setattr(env.services.market, "candles", boom)
    run(handlers.periodic_outcome_check(context(env)))


def test_on_error_notifies_user(env):
    from datetime import datetime as dt

    from telegram import Chat, Message, Update

    update = Update(update_id=1, message=Message(
        message_id=1, date=dt.now(), chat=Chat(id=CHAT_ID, type="private")))
    run(handlers.on_error(update, context(env, error=RuntimeError("упало"))))
    assert "Внутренняя ошибка" in env.bot.sent[-1]["text"]


def test_on_error_without_update_does_not_crash(env):
    run(handlers.on_error(None, context(env, error=RuntimeError("упало"))))
    assert env.bot.sent == []


def test_register_handlers_covers_all_buttons(env):
    application = FakeApplication()
    handlers.register_handlers(application)
    assert application.handlers, "хендлеры не зарегистрированы"
    # Команды + колбэк-кнобки + текст + ошибки.
    assert len(application.handlers) >= 8
