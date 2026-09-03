"""Bootstrap for the aiogram application."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from loguru import logger

from cryptoforge_pro.alerts import alert_loop
from cryptoforge_pro.analysis.engine import Scanner, SignalEngine
from cryptoforge_pro.config import Settings
from cryptoforge_pro.data.coinglass import CoinglassProvider
from cryptoforge_pro.data.exchanges import ExchangeRouter, build_exchange_router
from cryptoforge_pro.data.http import HttpSession
from cryptoforge_pro.data.news import NewsProvider
from cryptoforge_pro.data.sentiment import FearGreedProvider
from cryptoforge_pro.db import Database
from cryptoforge_pro.market import MarketService
from cryptoforge_pro.telegram import handlers
from cryptoforge_pro.telegram.context import BotContext
from cryptoforge_pro.telegram.handlers import router as main_router


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        self.http = HttpSession(timeout=settings.http_timeout)
        self.exchanges: ExchangeRouter = build_exchange_router(self.http, settings.exchange_list)
        self.coinglass = CoinglassProvider(
            self.http, settings.coinglass_api_key, settings.coinglass_base_url
        )
        self.news = NewsProvider(self.http, settings.cryptopanic_api_key, settings.news_language)
        self.fear_greed = FearGreedProvider(self.http)
        self.db = Database(settings.signals_db)
        self.market = MarketService(settings, self.exchanges, self.coinglass, self.news, self.fear_greed)
        self.engine = SignalEngine(settings)
        self.scanner = Scanner(settings, self.market, self.engine)
        self.context = BotContext(
            settings=settings,
            db=self.db,
            http=self.http,
            exchanges=self.exchanges,
            market=self.market,
            engine=self.engine,
            scanner=self.scanner,
        )

    async def start(self) -> None:
        await self.http.start()
        await self.db.init()
        handlers.set_context(self.context)

    async def stop(self) -> None:
        await self.db.close()
        await self.http.close()


async def _register_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="scan", description="Лучшие сетапы сейчас"),
            BotCommand(command="analyze", description="Глубокий анализ монеты"),
            BotCommand(command="search", description="Поиск по монете / условию"),
            BotCommand(command="market", description="Обзор рынка"),
            BotCommand(command="news", description="Новости рынка"),
            BotCommand(command="alerts", description="Ценовые алерты"),
            BotCommand(command="history", description="История идей"),
            BotCommand(command="risk", description="Риск-калькулятор"),
            BotCommand(command="status", description="Проверка доступа к данным"),
            BotCommand(command="settings", description="Настройки риска"),
        ]
    )


def create_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(main_router)
    return dp


async def run(settings: Settings) -> None:
    if not settings.telegram_token:
        raise SystemExit(
            "TELEGRAM_TOKEN is not configured. Set it in .env or Railway "
            "environment variables before starting CryptoForge Pro."
        )
    container = AppContainer(settings)
    await container.start()

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    try:
        await _register_commands(bot)
        me = await bot.get_me()
        logger.info("CryptoForge Pro started as @{}", me.username or "bot")
        dp = create_dispatcher(settings)
        watcher_task = asyncio.create_task(
            alert_loop(bot, container.market, container.db, settings.alert_check_interval_seconds)
        )
        try:
            await dp.start_polling(
                bot,
                allowed_updates=["message", "callback_query", "edited_message"],
                timeout=30,
            )
        finally:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
    finally:
        logger.info("Stopping CryptoForge Pro")
        await bot.session.close()
        await container.stop()
