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
from cryptopilot.liquidity import LiquidityTracker
from cryptopilot.live_radar import (
    Crossing,
    LiveRadar,
    active_flow_candidates,
    active_live_setups,
    refresh_watchlist,
)
from cryptopilot.models import CURRENT_PRIME_STRATEGY_VERSION, EarlySetup, Signal
from cryptopilot.prime_delivery import refresh_prime_entry
from cryptopilot.prime_shadow import PrimeShadowTracker
from cryptopilot.scanner import MarketScanner
from cryptopilot.smart_money import (
    SmartMoneyScanner,
    SmartMoneySetup,
    format_smart_money_setup,
    refresh_smart_money_watchlist,
)
from cryptopilot.squeeze_lab import SqueezeLab
from cryptopilot.storage import SignalStore
from cryptopilot.telegram import (
    build_router,
    format_early_setup,
    format_prime_setup,
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
    confirmation_exchange = None
    if settings.prime_cross_exchange_enabled:
        confirmation_name = "binance" if settings.exchange == "bybit" else "bybit"
        confirmation_exchange = build_exchange(
            confirmation_name,
            settings.bybit_base_url,
            settings.binance_base_url,
            settings.http_timeout_seconds,
            max(2, settings.request_concurrency // 2),
        )
    store = SignalStore(settings.database_path)
    await store.initialize()
    engine = SignalEngine(settings)
    scanner = MarketScanner(exchange, engine, store, settings)
    flow_tracker = FlowTracker()
    liquidity_tracker = LiquidityTracker()
    smart_money = SmartMoneyScanner(
        exchange,
        settings,
        flow_tracker,
        liquidity_tracker,
        confirmation_exchange,
    )
    flow_validator = (
        FlowForwardValidator(exchange, store, settings)
        if settings.flow_validation_enabled and exchange.name == "BYBIT"
        else None
    )
    prime_shadow = (
        PrimeShadowTracker(exchange, store, settings) if settings.prime_shadow_enabled else None
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
            f"Flow наблюдений: {live.flow_observed} · уведомлено: {live.flow_delivered} · "
            f"пропущено: {live.flow_dropped}\n"
            f"Ранний refresh: {settings.live_watchlist_interval_seconds} сек\n"
            f"Smart Money refresh: {settings.smart_money_scan_interval_seconds} сек\n\n"
            f"{validation}\n\n"
            "Flow использует publicTrade CVD-proxy и streaming OI; это наблюдение, не вход."
        )

    @router.message(Command("flowstats"))
    async def flow_stats(message: Message) -> None:
        early_stats, buildup_stats, absorption_stats = await asyncio.gather(
            store.flow_validation_stats(event_type="EARLY_PRESSURE"),
            store.flow_validation_stats(event_type="FLOW_BUILDUP"),
            store.flow_validation_stats(event_type="ABSORPTION"),
        )
        await message.answer(
            "<b>Forward-проверка PRE-MOVE Flow</b>\n"
            "EARLY_PRESSURE: "
            + _format_flow_validation(early_stats, settings.flow_validation_min_samples)
            + "\nFLOW_BUILDUP: "
            + _format_flow_validation(buildup_stats, settings.flow_validation_min_samples)
            + "\nABSORPTION: "
            + _format_flow_validation(absorption_stats, settings.flow_validation_min_samples)
            + "\n\nСобытия разделены, чтобы ранний EARLY_PRESSURE не смешивался "
            "с уже более зрелым FLOW_BUILDUP. Проверяется факт достижения trigger, "
            "а не гарантированная прибыльность."
        )

    @router.message(Command("primestats"))
    async def prime_stats(message: Message) -> None:
        stats = await store.prime_shadow_stats(strategy_version=CURRENT_PRIME_STRATEGY_VERSION)
        await message.answer(
            "<b>PRIME Shadow Learning</b>\n"
            + _format_prime_shadow_stats(stats, settings.prime_shadow_min_samples)
            + "\n\nЭта статистика считается по молча сохранённым PRIME-планам. "
            "Она не гарантирует будущий результат, но показывает, даёт ли стратегия "
            "положительный edge на новых данных."
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
                BotCommand(command="search", description="Единый поиск подходящих PRIME-монет"),
                BotCommand(command="scan", description="Единый поиск (тот же результат)"),
                BotCommand(command="early", description="Радар до импульса"),
                BotCommand(
                    command="smartmoney",
                    description="Единый поиск (прежняя команда Smart Money)",
                ),
                BotCommand(
                    command="prime",
                    description="Единый поиск (прежняя команда PRIME)",
                ),
                BotCommand(command="analyze", description="PRE-MOVE анализ одной монеты"),
                BotCommand(command="best", description="Единый поиск подходящих монет"),
                BotCommand(command="performance", description="Paper-статистика"),
                BotCommand(command="status", description="Версия и состояние"),
                BotCommand(command="live", description="Состояние потокового радара"),
                BotCommand(command="flowstats", description="Forward-проверка ранних Flow алертов"),
                BotCommand(command="primestats", description="Реальная Shadow-статистика PRIME"),
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
                    "Режим: PRE-MOVE first. PRIME = Spot + liquidity + cross-exchange + "
                    "ранний Flow + торговый план.\n"
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

        async def record_prime_shadow_candidate(item: SmartMoneySetup) -> None:
            if (
                not settings.prime_shadow_enabled
                or item.plan is None
                or item.bias.value == "NO_TRADE"
            ):
                return
            await store.record_prime_shadow(
                symbol=item.symbol,
                exchange=item.exchange,
                side=item.bias,
                score=item.prime_score,
                strategy_version=CURRENT_PRIME_STRATEGY_VERSION,
                created_at=item.created_at,
                entry_low=item.plan.entry_low,
                entry_high=item.plan.entry_high,
                stop_loss=item.plan.stop_loss,
                take_profit=item.plan.take_profit_2,
                entry_expires_at=item.plan.expires_at,
                max_holding_hours=settings.prime_shadow_max_holding_hours,
                dedup_minutes=settings.prime_shadow_dedup_minutes,
            )

        async def send_prepare_candidate(item: SmartMoneySetup) -> None:
            if (
                not settings.prepare_alerts_enabled
                or item.prime_ready
                or item.prime_score < settings.prepare_min_score
            ):
                return
            fingerprint = f"PREPARE:{item.symbol}:{item.bias.value}"
            symbol_allowed = await store.strict_alert_allowed(
                fingerprint,
                settings.prepare_symbol_cooldown_minutes,
            )
            budget_allowed = await store.notification_budget_available(
                "prepare",
                cooldown_minutes=settings.prepare_global_cooldown_minutes,
                max_per_day=settings.prepare_max_alerts_per_day,
            )
            if not symbol_allowed or not budget_allowed:
                return

            text_message = (
                "🟡 <b>PRE-MOVE ПОДГОТОВКА — ещё не PRIME-вход</b>\n"
                "Бот нашёл сильную раннюю конфигурацию до структурного пробоя. "
                "Монета уже под live-наблюдением; усиление Flow/OI запустит "
                "мгновенный повторный PRIME-анализ.\n\n"
                + format_smart_money_setup(item)
            )
            successes = 0
            for chat_id in settings.allowed_chat_ids:
                try:
                    await bot.send_message(chat_id, text_message)
                    successes += 1
                except Exception:
                    log.warning("Prepare candidate delivery failed for an authorized chat")
            if not successes:
                raise RuntimeError("No prepare alert recipients accepted the message")
            await store.mark_event_alerted(fingerprint, item.price)
            await store.mark_notification_budget("prepare")
            health.alerts_total += 1

        async def send_prime_candidate(item: SmartMoneySetup) -> None:
            if not settings.prime_alerts_enabled or not item.prime_ready:
                return
            if item.prime_score < settings.prime_min_score:
                return

            fingerprint = f"PRIME:{item.symbol}:{item.bias.value}"
            symbol_allowed = await store.strict_alert_allowed(
                fingerprint,
                settings.prime_symbol_cooldown_minutes,
            )
            budget_allowed = await store.notification_budget_available(
                "prime",
                cooldown_minutes=settings.prime_global_cooldown_minutes,
                max_per_day=settings.prime_max_alerts_per_day,
            )
            if not symbol_allowed or not budget_allowed:
                return

            item = await refresh_prime_entry(item, exchange, settings)
            if not item.prime_ready:
                return

            # Every PRIME that can reach the user must also enter forward Shadow
            # evaluation, including event-driven signals discovered between REST scans.
            await record_prime_shadow_candidate(item)
            message_text = format_prime_setup(item)

            successes = 0
            for chat_id in settings.allowed_chat_ids:
                try:
                    await bot.send_message(chat_id, message_text)
                    successes += 1
                except Exception:
                    log.exception("Prime candidate delivery failed")
            if successes == 0:
                raise RuntimeError("No prime alert recipients accepted the message")

            await store.mark_event_alerted(fingerprint, item.price)
            await store.mark_notification_budget("prime")
            if settings.flow_validation_enabled:
                await store.record_flow_observation(
                    symbol=item.symbol,
                    bias=item.bias,
                    score=item.prime_score,
                    event_type="PRIME_PREMOVE",
                    event_price=item.price,
                    trigger_price=item.trigger_price,
                    created_at=item.created_at,
                    window_minutes=settings.flow_validation_window_minutes,
                )
            health.alerts_total += 1

        async def handle_smart_money_report(_report) -> None:
            if settings.prime_shadow_enabled:
                for item in smart_money.shadow_candidates():
                    await record_prime_shadow_candidate(item)
            candidates = smart_money.prime_candidates()
            if candidates:
                await send_prime_candidate(candidates[0])
                return
            prepare = smart_money.prepare_candidates()
            if prepare:
                await send_prepare_candidate(prepare[0])

        async def recheck_prime_from_flow(event: FlowPressureEvent) -> None:
            # FLOW_BUILDUP can already be too late for PRIME. EARLY_PRESSURE and
            # absorption are the useful event-driven triggers for a fresh symbol check.
            if event.event_type not in {"EARLY_PRESSURE", "ABSORPTION"}:
                return
            setup = await smart_money.analyze_symbol(event.symbol)
            if setup is None or not setup.prime_ready:
                return
            await send_prime_candidate(setup)

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
            label = {
                "ABSORPTION": "ПОГЛОЩЕНИЕ",
                "EARLY_PRESSURE": "РАННЕЕ ДАВЛЕНИЕ",
            }.get(event.event_type, "НАРАСТАНИЕ ПОТОКА")
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
        if prime_shadow is not None:
            tasks.add(
                asyncio.create_task(
                    prime_shadow.run(stop_event),
                    name="prime-shadow-learning",
                )
            )
        if settings.smart_money_auto_scan_enabled:
            tasks.add(
                asyncio.create_task(
                    refresh_smart_money_watchlist(
                        smart_money,
                        stop_event,
                        settings.smart_money_scan_interval_seconds,
                        10,
                        handle_smart_money_report,
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
                    if settings.early_radar_enabled and settings.early_auto_alerts
                    else []
                ),
                send_crossing,
                store,
                flow_tracker=flow_tracker,
                liquidity_tracker=liquidity_tracker,
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
                    settings.flow_watchlist_limit,
                ),
                send_flow=send_flow_event,
                on_flow_observed=recheck_prime_from_flow,
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
        if confirmation_exchange is not None:
            await confirmation_exchange.close()
        await health_runner.cleanup()
        log.info("CryptoPilot stopped")


def _format_prime_shadow_stats(
    stats: dict[str, float | int | None],
    min_samples: int,
) -> str:
    sample = int(stats["sample_size"] or 0)
    resolved = int(stats["resolved"] or 0)
    pending = int(stats["pending"] or 0)
    no_entry = int(stats["no_entry"] or 0)
    if sample < min_samples:
        return (
            f"Закрытых PRIME-сделок: {sample}/{min_samples} · "
            f"resolved {resolved} · no-entry {no_entry} · pending {pending}. "
            "Пока рано менять веса стратегии по этой выборке."
        )
    win = stats["win_rate_pct"]
    expectancy = stats["expectancy_r"]
    factor = stats["profit_factor"]
    win_text = "н/д" if win is None else f"{float(win):.1f}%"
    expectancy_text = "н/д" if expectancy is None else f"{float(expectancy):+.2f}R"
    if factor is None:
        factor_text = "н/д"
    elif factor == float("inf"):
        factor_text = "∞"
    else:
        factor_text = f"{float(factor):.2f}"
    return (
        f"n={sample} · win {win_text} · expectancy {expectancy_text} · "
        f"PF {factor_text} · no-entry {no_entry} · pending {pending}"
    )


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
