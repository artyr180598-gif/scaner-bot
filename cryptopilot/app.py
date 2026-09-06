from __future__ import annotations

import asyncio
import html
import logging
import signal
import time
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, Message

from cryptopilot.config import get_settings
from cryptopilot.engine import SignalEngine
from cryptopilot.exchange import build_exchange
from cryptopilot.flow import FlowPressureEvent, FlowTracker
from cryptopilot.flow_validation import FlowForwardValidator
from cryptopilot.health import RuntimeHealth, start_health_server
from cryptopilot.live_radar import (
    Crossing,
    LiveRadar,
    active_flow_candidates,
    active_live_setups,
    refresh_watchlist,
)
from cryptopilot.models import EarlySetup, Signal
from cryptopilot.scanner import MarketScanner
from cryptopilot.smart_money import SmartMoneyScanner, refresh_smart_money_watchlist
from cryptopilot.squeeze_lab import SqueezeLab
from cryptopilot.storage import SignalStore
from cryptopilot.telegram import (
    build_router,
    format_early_setup,
    format_signal,
    main_keyboard,
    release_label,
)

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
    flow_tracker = FlowTracker()
    smart_money = SmartMoneyScanner(exchange, settings, flow_tracker)
    flow_validator = (
        FlowForwardValidator(exchange, store, settings)
        if settings.flow_validation_enabled and exchange.name == "BYBIT"
        else None
    )
    bot = Bot(
        settings.telegram_bot_token.strip(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    router = build_router(scanner, exchange, store, settings, health, smart_money)
    live: LiveRadar | None = None
    lab = SqueezeLab(exchange, store, settings) if settings.squeeze_lab_enabled else None
    if lab is not None:
        await lab.initialize()

    @router.message(Command("lab"))
    async def lab_status(message: Message) -> None:
        await message.answer(
            html.escape(await lab.report()) if lab is not None else "Лаборатория выключена."
        )

    @router.message(Command("live"))
    async def live_status(message: Message) -> None:
        if live is None:
            await message.answer("Потоковый радар выключен или не поддерживает выбранную биржу.")
            return
        age = (
            f"{max(0, int(time.time() * 1000) - live.last_trade_ms) / 1000:.1f} сек"
            if live.last_trade_ms is not None
            else "ещё не было"
        )
        stats = await store.flow_validation_stats()
        validation = _format_flow_validation(stats, settings.flow_validation_min_samples)
        await message.answer(
            f"<b>Потоковый радар</b>\nСоединение: {live.status}\n"
            f"Монет под наблюдением: {live.watching}\n"
            f"Последняя сделка в потоке: {age}\n"
            f"Пересечения: {live.delivered} · пропущено: {live.dropped}\n"
            f"Flow до BOS: {live.flow_delivered} · пропущено: {live.flow_dropped}\n"
            f"Ранний refresh: {settings.live_watchlist_interval_seconds} сек\n"
            f"Smart Money refresh: {settings.smart_money_scan_interval_seconds} сек\n\n"
            f"{validation}\n\n"
            "Flow использует publicTrade CVD-proxy и streaming OI; это наблюдение, не вход."
        )

    @router.message(Command("flowstats"))
    async def flow_stats(message: Message) -> None:
        stats = await store.flow_validation_stats()
        await message.answer(
            "<b>Forward-проверка раннего Flow</b>\n"
            + _format_flow_validation(stats, settings.flow_validation_min_samples)
            + "\n\nПроверяется только факт: был ли структурный trigger после раннего алерта. "
            "Это не win rate и не доказательство прибыльности."
        )

    dispatcher.include_router(router)
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
        await bot.set_my_commands(
            [
                BotCommand(command="menu", description="Обновить главное меню"),
                BotCommand(command="scan", description="Сканировать рынок"),
                BotCommand(command="early", description="Радар до импульса"),
                BotCommand(
                    command="smartmoney",
                    description="Крупный поток: структура + объём + OI",
                ),
                BotCommand(command="analyze", description="Анализ монеты"),
                BotCommand(command="best", description="Последние сильные сигналы"),
                BotCommand(command="performance", description="Paper-статистика"),
                BotCommand(command="status", description="Версия и состояние"),
                BotCommand(command="live", description="Состояние потокового радара"),
                BotCommand(command="flowstats", description="Forward-проверка ранних Flow алертов"),
                BotCommand(command="lab", description="Лаборатория сжатия: виртуальные сделки"),
                BotCommand(command="help", description="Как читать сигналы"),
            ]
        )
        health.ready = True
        log.info(
            "CryptoPilot ready as @%s; exchange=%s; authorized_ids=%d",
            health.bot_username,
            exchange.name,
            len(settings.allowed_chat_ids),
        )
        for chat_id in settings.allowed_chat_ids:
            try:
                await bot.send_message(
                    chat_id,
                    f"✅ <b>CryptoPilot {release_label()} запущен</b>\n"
                    "Smart Money Radar усилен realtime CVD, OI acceleration и absorption.\n"
                    "Если панель скрыта, отправьте /menu.",
                    reply_markup=main_keyboard(),
                    disable_notification=True,
                )
            except Exception:
                log.exception("Failed to refresh Telegram keyboard for an authorized chat")

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

        async def send_early_alert(setup: EarlySetup) -> None:
            successes = 0
            for chat_id in settings.allowed_chat_ids:
                try:
                    await bot.send_message(
                        chat_id,
                        "⚡ <b>Ранний радар: подтверждённое наблюдение, не вход</b>",
                    )
                    await bot.send_message(chat_id, format_early_setup(setup))
                    successes += 1
                except Exception:
                    log.exception("Failed to deliver early alert to an authorized chat")
            if successes == 0:
                raise RuntimeError("No configured Telegram chat accepted the early alert")
            health.alerts_total += 1

        async def send_flow_event(event: FlowPressureEvent) -> None:
            snapshot = event.snapshot
            burst = (
                f"{snapshot.volume_burst_ratio:.2f}×"
                if snapshot.volume_burst_ratio is not None
                else "н/д"
            )
            oi_change = (
                f"{snapshot.oi_change_2m_pct:+.2f}%"
                if snapshot.oi_change_2m_pct is not None
                else "н/д"
            )
            oi_accel = (
                f"{snapshot.oi_acceleration_pct_per_min:+.3f}%/мин"
                if snapshot.oi_acceleration_pct_per_min is not None
                else "н/д"
            )
            reasons = "\n".join(f"• {html.escape(item)}" for item in event.reasons)
            label = "ПОГЛОЩЕНИЕ" if event.event_type == "ABSORPTION" else "НАРАСТАНИЕ ПОТОКА"
            message_text = (
                f"⚡ <b>{html.escape(event.symbol)} · {label} ДО BOS</b>\n"
                f"Сценарий: {event.bias.value} · Flow score: <b>{event.score}/100</b>\n"
                f"Цена: <code>{event.price:.8g}</code> · "
                f"структурный trigger: <code>{event.trigger_price:.8g}</code>\n"
                f"Δ 60s: {snapshot.delta_ratio_60s:+.0%} · "
                f"CVD proxy 5m: {snapshot.cvd_ratio_5m:+.0%}\n"
                f"Поток: ${snapshot.notional_60s:,.0f}/60s · burst: {burst}\n"
                f"OI ~2m: {oi_change} · acceleration: {oi_accel}\n"
                + (
                    f"Absorption: {snapshot.absorption}\n"
                    if snapshot.absorption is not None
                    else ""
                )
                + f"\n<b>Почему сработало</b>\n{reasons}\n\n"
                "🔵 Это раннее наблюдение ДО структурного пробоя. "
                "Не входить только из-за этого сообщения: ждём подтверждение структуры/триггера."
            )
            successes = 0
            for chat_id in settings.allowed_chat_ids:
                try:
                    await bot.send_message(chat_id, message_text)
                    successes += 1
                except Exception:
                    log.warning("Flow event delivery failed for an authorized chat")
            if not successes:
                raise RuntimeError("No flow event recipients accepted the message")
            health.alerts_total += 1

        polling = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False), name="telegram-polling"
        )
        monitoring = asyncio.create_task(
            scanner.monitor(send_alert, send_early_alert, stop_event),
            name="market-monitor",
        )
        stopper = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
        tasks = {polling, monitoring, stopper}
        if flow_validator is not None:
            tasks.add(
                asyncio.create_task(
                    flow_validator.run(stop_event),
                    name="flow-forward-validation",
                )
            )
        if settings.smart_money_auto_scan_enabled:
            tasks.add(
                asyncio.create_task(
                    refresh_smart_money_watchlist(
                        smart_money,
                        stop_event,
                        settings.smart_money_scan_interval_seconds,
                    ),
                    name="smart-money-watchlist-refresh",
                )
            )
        if lab is not None:
            tasks.add(asyncio.create_task(lab.run(stop_event), name="squeeze-forward-lab"))
        if (
            settings.live_radar_enabled
            and exchange.name == "BYBIT"
            and (settings.early_radar_enabled or settings.flow_radar_enabled)
        ):

            async def send_crossing(event: Crossing) -> None:
                successes = 0
                for chat_id in settings.allowed_chat_ids:
                    try:
                        await bot.send_message(
                            chat_id,
                            f"⚡ <b>{html.escape(event.symbol)} · ПЕРЕСЕЧЕНИЕ УРОВНЯ</b>\n"
                            f"Направление пересечения: {event.direction}\n"
                            f"Уровень: {event.level:.8g} · сделка на бирже: {event.price:.8g}\n"
                            "Возраст события при получении: "
                            f"{event.received_ms - event.event_ms} мс\n"
                            "Потоковое наблюдение за сценарием раннего радара.\n"
                            "⚠️ Это не подтверждённый вход: возможен ложный пробой. "
                            "Продолжение движения и прибыль не гарантированы. "
                            "Цена могла измениться за время доставки.",
                        )
                        successes += 1
                    except Exception:
                        log.warning("Live event delivery failed for an authorized chat")
                if not successes:
                    raise RuntimeError("No live event recipients accepted the message")

            live = LiveRadar(
                lambda: (
                    active_live_setups(
                        scanner.last_early_report,
                        time.time(),
                        settings.min_early_auto_readiness,
                        2 * settings.live_watchlist_interval_seconds,
                    )
                    if settings.early_radar_enabled
                    else []
                ),
                send_crossing,
                store,
                flow_tracker=flow_tracker,
                flow_candidates=lambda: active_flow_candidates(
                    scanner.last_early_report,
                    smart_money.last_report,
                    time.time(),
                    2
                    * max(
                        settings.live_watchlist_interval_seconds,
                        settings.smart_money_scan_interval_seconds,
                    ),
                    smart_money.flow_watchlist(),
                ),
                send_flow=send_flow_event,
                settings=settings,
            )
            tasks.add(asyncio.create_task(live.run(stop_event), name="live-level-radar"))
            if settings.early_radar_enabled:
                tasks.add(
                    asyncio.create_task(
                        refresh_watchlist(
                            scanner, stop_event, settings.live_watchlist_interval_seconds
                        ),
                        name="live-watchlist-refresh",
                    )
                )
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
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



def _format_flow_validation(
    stats: dict[str, float | int | None],
    min_samples: int,
) -> str:
    resolved = int(stats["resolved"] or 0)
    pending = int(stats["pending"] or 0)
    if resolved < min_samples:
        return (
            f"Forward validation: {resolved}/{min_samples} завершённых наблюдений · "
            f"pending {pending}. Пока данных мало для оценки качества раннего обнаружения."
        )
    rate = stats["trigger_rate_pct"]
    lead = stats["median_lead_seconds"]
    rate_text = "н/д" if rate is None else f"{float(rate):.1f}%"
    lead_text = "н/д" if lead is None else f"{float(lead) / 60:.1f} мин"
    return (
        f"Forward validation: n={resolved} · trigger rate {rate_text} · "
        f"median lead {lead_text} · pending {pending}"
    )


def main() -> None:
    asyncio.run(run())
