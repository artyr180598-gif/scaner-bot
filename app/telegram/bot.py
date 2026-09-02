"""
app/telegram/bot.py — жизненный цикл Telegram-бота.

Запуск через long polling (не нужен публичный IP и webhook). Обработка
SIGTERM/SIGINT — корректное завершение, чтобы redeploy на Railway/Docker
не рвал сканирование на середине.
"""

from __future__ import annotations

import asyncio
import logging
import signal as os_signal
from typing import Optional

from telegram import Update
from telegram.ext import Application

from app.config.settings import Settings
from app.presentation import render
from app.telegram.handlers import (periodic_outcome_check, periodic_scan,
                                   register_handlers)
from app.telegram.services import BotServices

log = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 20


async def run_bot(settings: Settings, services: BotServices,
                  stop_event: Optional[asyncio.Event] = None) -> None:
    """Основной цикл бота. Возвращается после сигнала остановки."""
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан — бот не может стартовать")

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    application.bot_data["services"] = services
    register_handlers(application)

    await application.initialize()
    await application.start()

    # Периодические задачи.
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            periodic_scan,
            interval=max(300, settings.scan_interval_minutes * 60),
            first=max(60, settings.scan_interval_minutes * 60),
            name="periodic-scan",
        )
        application.job_queue.run_repeating(
            periodic_outcome_check, interval=1800, first=600,
            name="outcome-check",
        )
        log.info("job queue: скан каждые %d мин, проверка исходов каждые 30 мин",
                 settings.scan_interval_minutes)
    else:  # pragma: no cover — зависит от установленных пакетов
        log.warning("job queue недоступен — периодическое сканирование выключено")

    updater = application.updater
    if updater is None:  # pragma: no cover
        raise RuntimeError("updater недоступен: проверьте установку python-telegram-bot")

    await updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=5,
    )
    me = await application.bot.get_me()
    log.info("бот запущен: @%s (id=%s)", me.username, me.id)

    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover — не Linux
            pass

    if settings.startup_scan:
        application.create_task(_startup_scan(application, services, settings))

    await stop_event.wait()
    log.info("получен сигнал остановки — завершаю бота")
    await application.stop()
    await application.shutdown()


async def _startup_scan(application: Application, services: BotServices,
                        settings: Settings) -> None:
    """
    Первый скан сразу после старта: пользователь не должен ждать полчаса,
    чтобы увидеть, что бот вообще жив.
    """
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    if services.scanner.is_scanning:
        return
    log.info("стартовое сканирование рынка…")
    try:
        report = await services.scanner.run_scan()
    except Exception:  # noqa: BLE001
        log.exception("стартовое сканирование упало")
        return
    log.info("стартовый скан: %d сигналов из %d проверенных",
             len(report.actionable), report.scanned)
    if not settings.auto_push:
        return
    for chat_id in services.store.subscribed_chats():
        top = report.top(min(3, settings.top_n))
        for signal in top:
            try:
                from app.presentation import keyboards as kb

                await application.bot.send_message(
                    chat_id=chat_id,
                    text=render.render_signal(
                        signal,
                        deposit=float(services.store.settings(chat_id).deposit_usd)),
                    reply_markup=kb.signal_actions(signal.symbol),
                    disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("стартовый пуш в %s не удался: %s", chat_id, exc)
