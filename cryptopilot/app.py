from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage

from cryptopilot.config import get_settings
from cryptopilot.engine import SignalEngine
from cryptopilot.exchange import build_exchange
from cryptopilot.health import RuntimeHealth, start_health_server
from cryptopilot.models import Signal
from cryptopilot.scanner import MarketScanner
from cryptopilot.storage import SignalStore
from cryptopilot.telegram import build_router, format_signal

log = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
    )
    if not settings.telegram_bot_token.strip():
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing")
    if not settings.allowed_chat_ids:
        raise SystemExit("TELEGRAM_CHAT_ID is missing or empty")
    settings.prepare_runtime()

    health = RuntimeHealth(exchange=settings.exchange.upper())
    health_runner = await start_health_server(health, settings.port)
    exchange = build_exchange(
        settings.exchange,
        settings.bybit_base_url,
        settings.binance_base_url,
        settings.http_timeout_seconds,
        settings.request_concurrency,
    )
    store = SignalStore(settings.database_path)
    await store.initialize()
    engine = SignalEngine(settings)
    scanner = MarketScanner(exchange, engine, store, settings)
    bot = Bot(
        settings.telegram_bot_token.strip(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_router(scanner, exchange, store, settings, health))
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(event, stop_event.set)

    try:
        me = await bot.get_me()
        health.bot_username = me.username or str(me.id)
        await bot.delete_webhook(drop_pending_updates=False)
        if not await exchange.ping():
            raise RuntimeError(f"{exchange.name} health check failed")
        health.ready = True
        log.info(
            "CryptoPilot ready as @%s; exchange=%s; authorized_ids=%d",
            health.bot_username,
            exchange.name,
            len(settings.allowed_chat_ids),
        )

        async def send_alert(signal_item: Signal) -> None:
            successes = 0
            for chat_id in settings.allowed_chat_ids:
                try:
                    await bot.send_message(chat_id, "🚨 <b>Автоматически найден сильный сетап</b>")
                    await bot.send_message(chat_id, format_signal(signal_item))
                    successes += 1
                except Exception:
                    log.exception("Failed to deliver alert to an authorized chat")
            if successes == 0:
                raise RuntimeError("No configured Telegram chat accepted the alert")
            health.alerts_total += 1

        polling = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False), name="telegram-polling"
        )
        monitoring = asyncio.create_task(
            scanner.monitor(send_alert, stop_event), name="market-monitor"
        )
        stopper = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
        done, pending = await asyncio.wait(
            {polling, monitoring, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()
        stop_event.set()
        with suppress(RuntimeError):
            await dispatcher.stop_polling()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except TelegramUnauthorizedError as exc:
        raise SystemExit("Telegram rejected TELEGRAM_BOT_TOKEN") from exc
    finally:
        health.ready = False
        await dispatcher.storage.close()
        await bot.session.close()
        await exchange.close()
        await health_runner.cleanup()
        log.info("CryptoPilot stopped")


def main() -> None:
    asyncio.run(run())
