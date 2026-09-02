"""
app/telegram/handlers.py — обработчики команд и кнопок.

Слой тонкий: никаких индикаторов и бирж, только
«получил запрос → вызвал сервис → отрендерил → ответил».

Долгие операции (сканирование рынка) запускаются фоновой задачей, а сообщение
редактируется по завершении — иначе пользователь 3 минуты смотрит в пустоту.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, CallbackContext, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from app.domain.models import Direction, ScanReport, Signal
from app.presentation import keyboards as kb
from app.presentation import render
from app.presentation.format import base_of
from app.services.scanner import normalize_symbol
from app.telegram.services import BotServices

log = logging.getLogger(__name__)

PENDING_COIN_KEY = "awaiting_coin"


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _services(context: CallbackContext) -> BotServices:
    return context.application.bot_data["services"]


def _settings(context: CallbackContext):
    services = _services(context)
    chat_id = context.effective_chat.id if context.effective_chat else 0
    return services.store.settings(chat_id)


async def _apply_user_settings(context: CallbackContext) -> None:
    """
    Пользовательские настройки влияют на движок (пороги, депозит, ТФ).

    Движок один на всех, поэтому настройки применяются к копии конфигурации
    перед анализом по запросу конкретного чата.
    """
    services = _services(context)
    user = _settings(context)
    engine = services.engine
    engine.settings.min_confidence = float(user.min_confidence)
    engine.settings.min_rr = float(user.min_rr)
    engine.filter_config.min_confidence = float(user.min_confidence)
    engine.filter_config.min_rr = float(user.min_rr)
    try:
        from app.domain.models import Timeframe

        engine.settings.signal_timeframe = Timeframe.parse(user.signal_timeframe)
    except ValueError:
        pass
    from app.signals.planner import plan_from_config

    engine.plan_config = plan_from_config(engine.settings)
    engine.plan_config.risk_pct = float(engine.settings.risk_per_trade_pct)


async def _safe_answer(query) -> None:
    try:
        await query.answer()
    except TelegramError:
        pass


async def _edit_or_send(context: CallbackContext, text: str, reply_markup=None):
    """Редактирует сообщение кнопки или отправляет новое (для команд)."""
    query = context.callback_query
    if query is not None and query.message is not None:
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            log.debug("не удалось отредактировать сообщение: %s", exc)
    await context.bot.send_message(
        chat_id=context.effective_chat.id, text=text, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: CallbackContext) -> None:
    services = _services(context)
    report = services.scanner.last_report
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=render.render_menu(report),
        reply_markup=kb.main_menu(),
    )


async def cmd_help(update: Update, context: CallbackContext) -> None:
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=render.render_help(),
        reply_markup=kb.menu_row())


async def cmd_scan(update: Update, context: CallbackContext) -> None:
    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=render.render_progress("Запускаю сканирование рынка…", 0.02),
    )
    await start_scan(context, progress_message_id=message.message_id)


async def cmd_top(update: Update, context: CallbackContext) -> None:
    await show_top(context, send_new=True)


async def cmd_market(update: Update, context: CallbackContext) -> None:
    await show_market(context, send_new=True)


async def cmd_stats(update: Update, context: CallbackContext) -> None:
    services = _services(context)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=render.render_stats(services.journal.stats(), services.scanner.stats(),
                                 services.market.health()),
        reply_markup=kb.menu_row())


async def cmd_health(update: Update, context: CallbackContext) -> None:
    services = _services(context)
    candles = services.market.candles_cache.stats()
    lines = [
        "🩺 <b>Состояние системы</b>",
        "────────────────────",
    ]
    lines += [f"  {h}" for h in services.market.health()]
    lines += [
        f"  кэш свечей: {candles['size']} записей, hit rate "
        f"{candles['hit_rate'] * 100:.0f}%",
        f"  последний скан: {services.scanner.stats().get('finished_at', 'ещё не было')}",
        f"  новостей: {'вкл' if services.news.enabled else 'выкл'}"
        f" (ошибок: {services.news.errors})",
    ]
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text="\n".join(lines), reply_markup=kb.menu_row())


async def cmd_settings(update: Update, context: CallbackContext) -> None:
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=render.render_settings(_settings(context)),
        reply_markup=kb.settings_keyboard(_settings(context)))


async def cmd_coin(update: Update, context: CallbackContext) -> None:
    args = context.args or []
    if not args:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Введите тикер монеты, например: <code>BTC</code>, <code>SOL</code> "
                 "или <code>SOL/USDT</code>",
            reply_markup=kb.coin_picker(_services(context).scanner.last_report))
        context.chat_data[PENDING_COIN_KEY] = True
        return
    await analyze_and_send(context, " ".join(args))


async def cmd_watch(update: Update, context: CallbackContext) -> None:
    services = _services(context)
    chat_id = update.effective_chat.id
    args = context.args or []
    if args:
        symbol = normalize_symbol(args[0], services.settings.quote)
        added = await services.store.watch_add(chat_id, symbol)
        text = (f"⭐ <b>${base_of(symbol)}</b> добавлена в список наблюдения"
                if added else f"ℹ️ <b>${base_of(symbol)}</b> уже в списке")
        await context.bot.send_message(chat_id=chat_id, text=text)
        return
    symbols = services.store.watchlist(chat_id)
    await context.bot.send_message(
        chat_id=chat_id, text=render.render_watchlist(symbols),
        reply_markup=kb.watchlist_keyboard(symbols))


async def cmd_cancel(update: Update, context: CallbackContext) -> None:
    context.chat_data.pop(PENDING_COIN_KEY, None)
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text="Отменено.", reply_markup=kb.main_menu())


async def on_text(update: Update, context: CallbackContext) -> None:
    """Свободный ввод текста = тикер монеты (бот полностью кнопочный, но так удобнее)."""
    if not context.chat_data.get(PENDING_COIN_KEY):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Понимаю тикеры монет и команды. Нажмите кнопку меню ниже 👇",
            reply_markup=kb.main_menu())
        return
    context.chat_data.pop(PENDING_COIN_KEY, None)
    await analyze_and_send(context, update.message.text or "")


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------

async def on_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await _safe_answer(query)
    data = query.data

    try:
        if data == kb.CB_MENU:
            await _edit_or_send(context, render.render_menu(
                _services(context).scanner.last_report), kb.main_menu())
        elif data == kb.CB_SCAN:
            await start_scan(context, edit_current=True)
        elif data == kb.CB_TOP or data.startswith("top"):
            await show_top(context)
        elif data == kb.CB_MARKET:
            await show_market(context)
        elif data == kb.CB_HELP:
            await _edit_or_send(context, render.render_help(), kb.menu_row())
        elif data == kb.CB_STATS:
            services = _services(context)
            await _edit_or_send(context, render.render_stats(
                services.journal.stats(), services.scanner.stats(),
                services.market.health()), kb.menu_row())
        elif data == kb.CB_HEALTH:
            await cmd_health(update, context)
        elif data == "pick":
            await _edit_or_send(context, render.render_analysis_choice(
                _services(context).scanner.last_report),
                kb.coin_picker(_services(context).scanner.last_report))
            context.chat_data[PENDING_COIN_KEY] = True
        elif data.startswith("coin:"):
            await analyze_and_send(context, data.split(":", 1)[1])
        elif data.startswith("sig:"):
            await show_signal_by_index(context, int(data.split(":", 1)[1]))
        elif data.startswith("deep:"):
            await show_signal_by_index(context, int(data.split(":", 1)[1]), deep=True)
        elif data == kb.CB_WATCH:
            await show_watchlist(context)
        elif data.startswith("wl+:"):
            await toggle_watch(context, data.split(":", 1)[1], add=True)
        elif data.startswith("wl-:"):
            await toggle_watch(context, data.split(":", 1)[1], add=False)
        elif data == kb.cb_watch_clear():
            services = _services(context)
            await services.store.watch_clear(context.effective_chat.id)
            await show_watchlist(context)
        elif data.startswith("set:"):
            await change_setting(context, data)
        elif data == kb.CB_SETTINGS:
            await _edit_or_send(context, render.render_settings(_settings(context)),
                                kb.settings_keyboard(_settings(context)))
        elif data == "noop":
            return
        else:
            await _edit_or_send(context, render.render_menu(
                _services(context).scanner.last_report), kb.main_menu())
    except Exception as exc:  # noqa: BLE001 — кнопка не должна ронять бота
        log.exception("обработчик кнопки %s упал: %s", data, exc)
        await _edit_or_send(
            context,
            f"⚠️ Что-то пошло не так при обработке кнопки: {type(exc).__name__}.\n"
            f"Попробуйте ещё раз или вернитесь в меню.",
            kb.main_menu())


# ---------------------------------------------------------------------------
# Действия
# ---------------------------------------------------------------------------

async def start_scan(context: CallbackContext, progress_message_id: Optional[int] = None,
                     edit_current: bool = False) -> None:
    """Запускает сканирование фоновой задачей и редактирует сообщение по ходу."""
    services = _services(context)
    chat_id = context.effective_chat.id

    if services.scanner.is_scanning:
        await _edit_or_send(
            context,
            "⏳ Сканирование уже идёт — покажу результат, как только закончу.\n"
            "Обычно это 2–4 минуты.", kb.menu_row())
        return

    message_id = progress_message_id
    if edit_current and context.callback_query and context.callback_query.message:
        message_id = context.callback_query.message.message_id
    if message_id is None:
        message = await context.bot.send_message(
            chat_id=chat_id, text=render.render_progress("Готовлюсь…", 0.01))
        message_id = message.message_id

    await _apply_user_settings(context)

    # Задача прогресса может проснуться позже финального результата — тогда
    # она не должна перетирать готовый ответ («прогресс» поверх списка).
    state = {"finished": False}

    def progress(text: str, ratio: float) -> None:
        # Прогресс приходит из рабочего потока — отправляем через create_task.
        context.application.create_task(_update_progress(
            context.bot, chat_id, message_id, text, ratio, state))

    async def _run() -> None:
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError:
            pass
        report = await services.scanner.run_scan(progress=progress)
        state["finished"] = True
        await _publish_scan_result(context, report, message_id)

    context.application.create_task(_run())


async def _update_progress(bot, chat_id: int, message_id: int, text: str,
                           ratio: float, state: Optional[Dict[str, bool]] = None
                           ) -> None:
    if state and state.get("finished"):
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=render.render_progress(text, ratio))
    except (BadRequest, TelegramError) as exc:
        log.debug("прогресс не обновлён: %s", exc)


async def _publish_scan_result(context: CallbackContext, report: ScanReport,
                               message_id: int) -> None:
    services = _services(context)
    chat_id = context.effective_chat.id
    top = report.top(services.settings.top_n)
    if not top:
        text = render.render_top_signals(report, limit=services.settings.top_n)
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                                text=text, reply_markup=kb.menu_row())
        except BadRequest:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           reply_markup=kb.menu_row())
        return

    for signal in top:
        if signal.direction is Direction.WAIT:
            continue
        if services.journal.is_recent(
                signal.symbol, signal.direction.value,
                services.settings.watchlist_cooldown_minutes):
            continue
        await services.journal.add(signal, potential=_signal_potential(signal))

    text = render.render_top_signals(report, limit=services.settings.top_n)
    keyboard = kb.signals_keyboard(top)
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                            text=text, reply_markup=keyboard)
    except BadRequest:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

    await push_to_subscribers(context, report)


async def push_to_subscribers(context: CallbackContext, report: ScanReport) -> None:
    """Авто-рассылка свежих сильных сигналов подписанным чатам."""
    services = _services(context)
    if not services.settings.auto_push:
        return
    chats = services.store.subscribed_chats()
    if not chats:
        return
    fresh = [s for s in report.actionable
             if s.confidence >= services.settings.push_min_confidence]
    if not fresh:
        return
    for chat_id in chats:
        if chat_id == context.effective_chat.id:
            continue        # этому чату уже показали результат
        for signal in fresh[:3]:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=render.render_signal(signal, deposit=_deposit_of(services, chat_id)),
                    reply_markup=kb.signal_actions(signal.symbol),
                    disable_web_page_preview=True)
            except TelegramError as exc:
                log.warning("авто-пуш в %s не удался: %s", chat_id, exc)


def _deposit_of(services: BotServices, chat_id: int) -> float:
    return float(services.store.settings(chat_id).deposit_usd)


def _signal_potential(signal: Signal) -> float:
    from app.analysis.base import Group

    factors = signal.factors.by_group().get(Group.POTENTIAL) or []
    if not factors:
        return 0.5
    weights = sum(f.weight for f in factors) or len(factors)
    return round(sum(f.score * f.weight for f in factors) / weights, 3)


async def show_top(context: CallbackContext, send_new: bool = False) -> None:
    services = _services(context)
    report = services.scanner.last_report
    if report is None:
        if send_new:
            await context.bot.send_message(
                chat_id=context.effective_chat.id,
                text="Сканирования ещё не было — запускаю…")
        await start_scan(context, edit_current=not send_new)
        return
    top = report.top(10)
    text = render.render_top_signals(report, limit=10)
    keyboard = kb.signals_keyboard(top) if top else kb.menu_row()
    if send_new:
        await context.bot.send_message(chat_id=context.effective_chat.id, text=text,
                                       reply_markup=keyboard)
    else:
        await _edit_or_send(context, text, keyboard)


async def show_market(context: CallbackContext, send_new: bool = False) -> None:
    services = _services(context)
    report = services.scanner.last_report
    if report is None:
        await start_scan(context, edit_current=not send_new)
        return
    text = render.render_scanner(report)
    keyboard = kb.coin_picker(report)
    if send_new:
        await context.bot.send_message(chat_id=context.effective_chat.id, text=text,
                                       reply_markup=keyboard)
    else:
        await _edit_or_send(context, text, keyboard)


async def show_watchlist(context: CallbackContext) -> None:
    services = _services(context)
    symbols = services.store.watchlist(context.effective_chat.id)
    await _edit_or_send(context, render.render_watchlist(symbols),
                        kb.watchlist_keyboard(symbols))


async def toggle_watch(context: CallbackContext, base: str, add: bool) -> None:
    services = _services(context)
    symbol = normalize_symbol(base, services.settings.quote)
    if add:
        await services.store.watch_add(context.effective_chat.id, symbol)
        text = f"⭐ <b>${base_of(symbol)}</b> добавлена в список наблюдения"
    else:
        await services.store.watch_remove(context.effective_chat.id, symbol)
        text = f"➖ <b>${base_of(symbol)}</b> убрана из списка"
    await _edit_or_send(context, text, kb.signal_actions(symbol, in_watchlist=add))


async def show_signal_by_index(context: CallbackContext, index: int,
                               deep: bool = False) -> None:
    services = _services(context)
    report = services.scanner.last_report
    if report is None:
        await _edit_or_send(context, "Сканирования ещё не было — запустите его.",
                            kb.main_menu())
        return
    top = report.top(14)
    if index < 0 or index >= len(top):
        await _edit_or_send(context, "Сигнал устарел — обновите список.", kb.menu_row())
        return
    signal = top[index]
    deposit = _deposit_of(services, context.effective_chat.id)
    text = (render.render_deep_analysis(signal, deposit=deposit) if deep
            else render.render_signal(signal, deposit=deposit,
                                      context=report.context))
    watch = services.store.watchlist(context.effective_chat.id)
    keyboard = kb.signal_actions(signal.symbol, in_watchlist=signal.symbol in watch)
    await _edit_or_send(context, text, keyboard)


async def analyze_and_send(context: CallbackContext, raw: str) -> None:
    """Глубокий анализ одной монеты по запросу."""
    services = _services(context)
    chat_id = context.effective_chat.id
    symbol = normalize_symbol(raw, services.settings.quote)
    if len(base_of(symbol)) < 2:
        await _edit_or_send(
            context, "Не понял тикер. Примеры: <code>BTC</code>, <code>SOL/USDT</code>.",
            kb.coin_picker(services.scanner.last_report))
        return

    await _apply_user_settings(context)
    progress = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔬 Анализирую <b>${base_of(symbol)}</b>…\n"
             f"Собираю свечи 4 таймфреймов, структуру, объёмы и деривативы.")
    try:
        signal = await services.scanner.analyze_symbol(symbol)
    except Exception as exc:  # noqa: BLE001
        log.exception("анализ %s не удался: %s", symbol, exc)
        try:
            await progress.edit_text(
                f"⚠️ Не удалось проанализировать ${base_of(symbol)}: {exc}\n"
                f"Проверьте тикер и повторите.")
        except BadRequest:
            pass
        return

    if signal.actionable and not services.journal.is_recent(
            signal.symbol, signal.direction.value, 60):
        await services.journal.add(signal, potential=_signal_potential(signal))

    deposit = _deposit_of(services, chat_id)
    watch = services.store.watchlist(chat_id)
    text = render.render_signal(signal, deposit=deposit)
    if _settings(context).show_beginner_hints and signal.actionable:
        from app.signals.explain import beginner_note

        text += "\n\n" + beginner_note(signal)
    try:
        await progress.edit_text(
            text, reply_markup=kb.signal_actions(signal.symbol,
                                                 in_watchlist=signal.symbol in watch),
            disable_web_page_preview=True)
    except BadRequest as exc:
        log.debug("не удалось отредактировать сообщение анализа: %s", exc)
        await context.bot.send_message(chat_id=chat_id, text=text,
                                       reply_markup=kb.signal_actions(signal.symbol))


async def change_setting(context: CallbackContext, data: str) -> None:
    """Обработка кнопок настроек: ``set:key:value``."""
    services = _services(context)
    chat_id = context.effective_chat.id
    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    _, key, value = parts
    settings = services.store.settings(chat_id)

    if key == "risk":
        settings.risk_profile = value
        from app.config.settings import RISK_PRESETS
        from app.domain.models import RiskProfile

        preset = RISK_PRESETS[RiskProfile.parse(value)]
        settings.min_confidence = float(preset["min_confidence"])
        settings.min_rr = float(preset["min_rr"])
    elif key == "tf":
        settings.signal_timeframe = value
    elif key == "conf":
        settings.min_confidence = float(value)
    elif key == "dep":
        settings.deposit_usd = float(value)
    elif key == "push":
        settings.auto_push = value == "1"
    elif key == "hints":
        settings.show_beginner_hints = value == "1"
    else:
        return

    await services.store.save_settings(chat_id, settings)
    await _edit_or_send(context, render.render_settings(settings),
                        kb.settings_keyboard(settings))


# ---------------------------------------------------------------------------
# Фоновые задачи
# ---------------------------------------------------------------------------

async def periodic_scan(context: CallbackContext) -> None:
    """Периодическое сканирование + авто-рассылка (job queue)."""
    services = _services(context)
    if services.scanner.is_scanning:
        log.info("периодический скан пропущен: предыдущий ещё идёт")
        return
    try:
        report = await services.scanner.run_scan()
    except Exception:  # noqa: BLE001
        log.exception("периодический скан упал")
        return
    await services.journal.update_outcomes(services.market)
    await push_to_subscribers(context, report)


async def periodic_outcome_check(context: CallbackContext) -> None:
    """Обновление исходов прошлых сигналов (для честной статистики /stats)."""
    services = _services(context)
    try:
        updated = await services.journal.update_outcomes(services.market)
        if updated:
            log.info("журнал: закрыто исходов — %d", updated)
    except Exception:  # noqa: BLE001
        log.exception("проверка исходов упала")


async def on_error(update: object, context: CallbackContext) -> None:
    log.error("ошибка обработчика: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Внутренняя ошибка. Подробности в логах бота.",
                reply_markup=kb.main_menu())
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------

def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler(["start", "menu"], cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("top", cmd_top))
    application.add_handler(CommandHandler("market", cmd_market))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("health", cmd_health))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("coin", cmd_coin))
    application.add_handler(CommandHandler(["watch", "wl"], cmd_watch))
    application.add_handler(CommandHandler(["cancel", "stop"], cmd_cancel))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)
