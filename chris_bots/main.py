"""
Точка входа Telegram-бота Крис.

Запуск:
    python -m chris_bots.main

Env:
    TELEGRAM_TOKEN=<обязательно>
    EXCHANGES=binance,bybit
    BASE_TIMEFRAME=1h
    ANALYSIS_TIMEFRAMES=1h,4h,1d
    MIN_CONFIDENCE=75
    ... (см. chris_bots.config.settings)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from contextlib import suppress
from typing import Any, Dict

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config.settings import get_settings, loaded_env_file, token_env_name
from .core.events import EventBus
from .data.exchange.gateway import close_gateway, get_gateway
from .data.storage.sqlite_store import SignalStore
from .scanner import ScannerEngine
from .telegram.handlers import all_routers
from .telegram.middlewares import AccessControlMiddleware
from .utils.logging import setup_logging

log = logging.getLogger(__name__)

# ── Глобальный state для хендлеров (через app_state[...]) ────
app_state: Dict[str, Any] = {}


async def main() -> int:
    settings = get_settings()

    # Логи настраиваем ДО валидации: иначе сообщение об ошибке уходит через
    # logging.lastResort — без времени и уровня (так и выглядело в логах).
    setup_logging(settings.log_level)

    env_file = loaded_env_file()
    if env_file:
        log.info("env file: %s", env_file)
    else:
        log.info("env file: не найден, беру только переменные окружения процесса")

    try:
        settings.validate()
    except ValueError as exc:
        log.critical("settings invalid: %s", exc)
        return 2

    log.info("chris_bots starting (token из %s=%s…, dry_run=%s)",
             token_env_name() or "—", settings.telegram_token[:8], settings.dry_run)

    # ── Инфраструктура ────────────────────────────────────────
    bus = EventBus()
    await bus.start()

    gateway = await get_gateway(settings.exchanges, settings.quote_currencies)
    log.info("exchanges online: %s", gateway.available())

    store = SignalStore(path=settings.signals_db)
    await store.start()

    engine = ScannerEngine(settings=settings, gateway=gateway, event_bus=bus)

    # ── Telegram ──────────────────────────────────────────────
    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.message.middleware(AccessControlMiddleware(settings))
    dp.callback_query.middleware(AccessControlMiddleware(settings))
    for r in all_routers():
        dp.include_router(r)

    # Передаём движок в app_state (используется в handlers).
    app_state.clear()
    app_state.update(
        engine=engine,
        gateway=gateway,
        bus=bus,
        store=store,
        settings=settings,
    )

    # ── Graceful shutdown ─────────────────────────────────────
    stop_event = asyncio.Event()

    def _stop(*_: Any) -> None:
        log.info("shutdown requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    # ── Polling ───────────────────────────────────────────────
    try:
        log.info("telegram: starting long polling")
        await dp.start_polling(bot, handle_signals=False, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("shutting down…")
        await bot.session.close()
        await engine.close()
        await store.close()
        await close_gateway()
        await bus.stop()
        log.info("bye 👋")

    return 0


def run() -> int:
    """Entry point для setup.py / poetry."""
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(run())
