"""aiogram handlers for CryptoForge Pro."""

from __future__ import annotations

from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from cryptoforge_pro.analysis.engine import Signal
from cryptoforge_pro.data.exchanges import MarketDataUnavailable
from cryptoforge_pro.models import MarketData
from cryptoforge_pro.telegram import format
from cryptoforge_pro.telegram.context import BotContext
from cryptoforge_pro.telegram.keyboards import (
    back_menu_keyboard,
    help_keyboard,
    main_menu_keyboard,
    order_keyboard,
    search_keyboard,
    settings_keyboard,
)
from cryptoforge_pro.telegram.states import NavStates
from cryptoforge_pro.text_parse import parse_query

router = Router()

_bot_context: Optional[BotContext] = None


def set_context(ctx: BotContext) -> None:
    global _bot_context
    _bot_context = ctx


def _ctx() -> BotContext:
    if _bot_context is None:
        raise RuntimeError("BotContext is not configured")
    return _bot_context


async def _authorized(message: Message | CallbackQuery) -> bool:
    settings = _ctx().settings
    chat_id = message.from_user.id if message.from_user else 0
    if settings.allowed_ids and chat_id not in settings.allowed_ids:
        if isinstance(message, Message):
            await message.answer("⛔️ Доступ ограничен. Обратитесь к администратору.")
        else:
            await message.answer("⛔️ Доступ ограничен.")
        return False
    return True


def _pretty_mode(mode: str) -> str:
    return {
        "best": "🔥 Лучшие сетапы",
        "long": "📈 Только лонги",
        "short": "📉 Только шорты",
        "scalp": "⚡ Скальп",
        "swing": "🎯 Свинг",
    }.get(mode, mode)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not await _authorized(message):
        return
    await state.clear()
    await message.answer(
        "🔥 <b>CryptoForge Pro</b> — крипто-помощник на реальных данных.\n\n"
        "Анализирую Binance и Bybit в реальном времени: структура, объём, RSI, ATR, "
        "Open Interest и funding. Никаких заглушек — если биржа недоступна, честно скажу.\n\n"
        "Выберите раздел:",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    if not await _authorized(message):
        return
    await state.clear()
    await message.answer("🏠 Главное меню:", reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    if not await _authorized(message):
        return
    await state.clear()
    await message.answer(
        "ℹ️ <b>Помощь CryptoForge Pro</b>\n\n"
        "• <b>/start</b> — меню\n"
        "• <b>/scan</b> — лучшие сетапы\n"
        "• <b>/analyze BTC</b> — глубокий анализ монеты\n"
        "• <b>/search ETH long 1h</b> — поиск по условию\n"
        "• <b>/settings</b> — риск-профиль и порог уверенности\n\n"
        "Команды можно вводить и обычным текстом, например:\n"
        "<code>BTC</code>, <code>долгий ETH 4h</code>, <code>short SOL 15m</code>",
        reply_markup=help_keyboard(),
    )


@router.message(Command("scan"))
async def cmd_scan(message: Message) -> None:
    if not await _authorized(message):
        return
    await _run_scan(message, mode="best", direction=None)


@router.message(Command("analyze"))
async def cmd_analyze(message: Message, state: FSMContext) -> None:
    if not await _authorized(message):
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await _handle_symbol(message, parts[1].strip())
    else:
        await state.set_state(NavStates.wait_symbol)
        await message.answer(
            "📊 Введите тикер для глубокого анализа.\n\n"
            "Например: <code>BTC</code>, <code>SOL</code>, <code>DOGEUSDT</code>, <code>ETH</code>",
            reply_markup=search_keyboard(),
        )


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    if not await _authorized(message):
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await _handle_search_query(message, parts[1].strip())
    else:
        await state.set_state(NavStates.wait_search_query)
        await message.answer(
            "🔍 Напишите монету или условие.\n\n"
            "Примеры:\n"
            "<code>BTC</code>\n"
            "<code>ETH long 1h</code>\n"
            "<code>short SOL 15m</code>\n"
            "<code>volatile altcoins volume > 100m</code>\n\n"
            "Если найду тикер — покажу полный разбор и сетап. Если нет — отсканирую рынок.",
            reply_markup=search_keyboard(),
        )


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    if not await _authorized(message):
        return
    settings = _ctx().settings
    await state.set_state(NavStates.settings)
    user = await _ctx().db.get_user(message.from_user.id)
    current = user["risk_profile"] if user else settings.default_risk_profile
    conf = (user or {}).get("min_confidence") or settings.min_confidence
    await message.answer(
        f"⚙️ <b>Настройки риска</b>\n\n"
        f"Профиль: <b>{current}</b>\n"
        f"Порог уверенности: <b>{conf}%</b>\n\n"
        "Выберите профиль:",
        reply_markup=settings_keyboard(current),
    )


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _authorized(callback):
        return
    await state.clear()
    await callback.message.edit_text("🏠 Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery) -> None:
    if not await _authorized(callback):
        return
    await callback.message.edit_text(
        "ℹ️ <b>Помощь CryptoForge Pro</b>\n\n"
        "• <code>/start</code> — меню\n"
        "• <code>/scan</code> — лучшие сетапы\n"
        "• <code>/analyze BTC</code> — глубокий анализ\n"
        "• <code>/search ETH long 1h</code> — поиск по условию\n"
        "• <code>/settings</code> — риск-профиль\n\n"
        "Все идеи — на реальных биржевых данных, без моков.",
        reply_markup=help_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "search")
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _authorized(callback):
        return
    await state.set_state(NavStates.wait_search_query)
    await callback.message.edit_text(
        "🔍 Напишите монету или условие (например: <code>ETH long 1h</code>, <code>BTC</code>).",
        reply_markup=search_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "analyze")
async def cb_analyze(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _authorized(callback):
        return
    await state.set_state(NavStates.wait_symbol)
    await callback.message.edit_text(
        "📊 Введите тикер для анализа. Например: <code>BTC</code>, <code>SOL</code>, <code>DOGE</code>.",
        reply_markup=search_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("analyze:"))
async def cb_analyze_symbol(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _authorized(callback):
        return
    symbol = callback.data.split(":", 1)[1]
    await state.clear()
    await callback.answer()
    await _handle_symbol(callback.message, symbol)


@router.callback_query(F.data.startswith("quick:"))
async def cb_quick(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _authorized(callback):
        return
    _, direction, symbol = callback.data.split(":", 2)
    await state.clear()
    await callback.answer()
    await _handle_symbol(callback.message, symbol, force_direction=direction.upper())


@router.callback_query(F.data.startswith("profile:"))
async def cb_profile(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _authorized(callback):
        return
    settings = _ctx().settings
    await state.set_state(NavStates.settings)
    profile = callback.data.split(":", 1)[1]
    user = await _ctx().db.get_user(callback.from_user.id)
    conf = (user or {}).get("min_confidence") or settings.min_confidence
    await _ctx().db.upsert_user(callback.from_user.id, profile, conf)
    await callback.message.edit_text(
        f"✅ Профиль риска: <b>{profile}</b>\n"
        f"Порог уверенности: <code>{conf}%</code>\n\n"
        "Теперь новые сканы будут использовать этот профиль.",
        reply_markup=settings_keyboard(profile),
    )
    await callback.answer()


@router.callback_query(F.data == "settings_confidence")
async def cb_settings_confidence(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _authorized(callback):
        return
    await state.set_state(NavStates.settings_confidence)
    await callback.message.edit_text(
        "🎚 Введите порог уверенности от <b>40</b> до <b>95</b> (например: <code>62</code>).\n\n"
        "Выше порог — меньше идей, но более отобранных.",
        reply_markup=settings_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("scan_"))
async def cb_scan(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _authorized(callback):
        return
    mode = callback.data.removeprefix("scan_")
    direction = None
    if mode == "long":
        direction = "LONG"
        mode = "best"
    elif mode == "short":
        direction = "SHORT"
        mode = "best"
    await state.clear()
    await callback.answer()
    await _run_scan(callback.message, mode=mode, direction=direction)


# ---------------------------------------------------------------------------
# FSM text
# ---------------------------------------------------------------------------

@router.message(NavStates.wait_symbol)
async def msg_wait_symbol(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _handle_symbol(message, message.text or "")


@router.message(NavStates.wait_search_query)
async def msg_wait_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _handle_search_query(message, message.text or "")


@router.message(NavStates.settings_confidence)
async def msg_confidence(message: Message, state: FSMContext) -> None:
    await state.clear()
    settings = _ctx().settings
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введите число, например <code>62</code>.", reply_markup=main_menu_keyboard())
        return
    if not 40 <= value <= 95:
        await message.answer("⚠️ Порог должен быть от 40 до 95.", reply_markup=main_menu_keyboard())
        return
    user = await _ctx().db.get_user(message.from_user.id)
    profile = (user or {}).get("risk_profile") or settings.default_risk_profile
    await _ctx().db.upsert_user(message.from_user.id, profile, value)
    await message.answer(f"✅ Порог уверенности: <b>{value}%</b>", reply_markup=main_menu_keyboard())


# Generic command-style text (not in FSM) — convenient for power users.
@router.message(Command("send_menu"))
async def cmd_send_menu(message: Message) -> None:
    await message.answer("🏠 Главное меню:", reply_markup=main_menu_keyboard())


# Not adding a catch-all text handler to avoid hijacking unrelated chats.
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_scan(message: Message, mode: str, direction: Optional[str]) -> None:
    ctx = _ctx()
    user = await ctx.db.get_user(message.from_user.id)
    profile = (user or {}).get("risk_profile") or ctx.settings.default_risk_profile
    display_mode = _pretty_mode(mode)
    if direction == "LONG":
        display_mode = "📈 Только лонги"
    elif direction == "SHORT":
        display_mode = "📉 Только шорты"
    progress = await message.answer(f"🔎 <b>{display_mode}</b>… Сканирую реальные данные Binance/Bybit, это занимает несколько секунд.")
    try:
        signals = await ctx.scanner.scan(
            mode=mode,
            direction=direction,
            limit=ctx.settings.top_scans,
            risk_profile=profile,
        )
    except MarketDataUnavailable as exc:
        await progress.edit_text(format.format_error(str(exc)))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("scan failed")
        await progress.edit_text(f"⚠️ Ошибка скана: <code>{format_escape(str(exc))}</code>")
        return

    if not signals:
        await progress.edit_text(
            f"{display_mode}: подходящих сетапов выше порога уверенности пока нет.\n\n"
            "Это честный ответ на реальных данных — лучше без сделки, чем с выдуманной.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await progress.edit_text(
        f"✅ <b>{display_mode}</b> — топ-{min(len(signals), ctx.settings.top_scans)} качественных сетапов.",
    )
    for sig in signals:
        text = format.format_signal(sig)
        kb = order_keyboard(sig.symbol, sig.base)
        try:
            await message.answer(text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            await message.answer(text, reply_markup=kb, parse_mode=None)
    await message.answer("⬇️ Ниже можно открыть глубокий анализ или мгновенно получить идею в конкретную сторону.", reply_markup=main_menu_keyboard())


async def _handle_symbol(
    message: Message,
    raw: str,
    force_direction: Optional[str] = None,
    timeframes: Optional[list[str]] = None,
    mode: str = "best",
) -> None:
    ctx = _ctx()
    symbol = _normalize_symbol(raw)
    if not symbol:
        await message.answer(
            "⚠️ Не могу распознать тикер. Напишите, например, <code>BTC</code> или <code>SOLUSDT</code>.",
            reply_markup=main_menu_keyboard(),
        )
        return
    tfs = timeframes or ["15m", "1h", "4h", "1d"]
    progress = await message.answer(f"🔍 Загружаю реальные данные {symbol}…")
    try:
        data: MarketData = await ctx.market.get_market_data(symbol, timeframes=tfs)
    except MarketDataUnavailable as exc:
        await progress.edit_text(format.format_error(str(exc)))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("analyze failed")
        await progress.edit_text(f"⚠️ Ошибка: <code>{format_escape(str(exc))}</code>")
        return

    user = await ctx.db.get_user(message.from_user.id)
    profile = (user or {}).get("risk_profile") or ctx.settings.default_risk_profile
    signal: Optional[Signal] = None
    if force_direction:
        signal = ctx.engine.build_signal(data, direction=force_direction, mode=mode, risk_profile=profile)
    else:
        signal = ctx.engine.build_signal(data, mode=mode, risk_profile=profile)
    deep = format.format_deep_analysis(data, ctx.settings)
    try:
        await progress.edit_text(deep, reply_markup=order_keyboard(data.symbol, data.base))
    except Exception:  # noqa: BLE001
        await progress.edit_text(deep)
    if signal:
        await message.answer(format.format_signal(signal), reply_markup=back_menu_keyboard())


async def _handle_search_query(message: Message, raw: str) -> None:
    ctx = _ctx()
    q = parse_query(raw)
    if q.symbol:
        if q.mode == "scalp":
            tfs = ["15m", "1h"]
        elif q.mode == "swing":
            tfs = ["4h", "1d"]
        else:
            tfs = ["15m", "1h", "4h", "1d"]
        await _handle_symbol(
            message,
            q.symbol,
            force_direction=q.direction,
            timeframes=tfs,
            mode=q.mode or "best",
        )
        return
    mode = q.mode or "best"
    direction = q.direction
    if direction is None and (q.min_volume_usd or q.max_atr):
        direction = None
    progress = await message.answer("🔎 Ищу по условию…")
    try:
        signals = await ctx.scanner.scan(
            mode=mode,
            direction=direction,
            limit=3,
            risk_profile="balanced",
            min_volume_usd=q.min_volume_usd,
            min_abs_change=q.min_abs_change,
            max_atr=q.max_atr,
        )
    except MarketDataUnavailable as exc:
        await progress.edit_text(format.format_error(str(exc)))
        return
    except Exception as exc:  # noqa: BLE001
        await progress.edit_text(f"⚠️ Ошибка: <code>{format_escape(str(exc))}</code>")
        return
    if not signals:
        await progress.edit_text(
            "Подходящих сетапов на реальных данных нет. Попробуйте другой режим или монету.",
            reply_markup=main_menu_keyboard(),
        )
        return
    await progress.edit_text("✅ Нашёл следующие идеи:")
    for sig in signals:
        await message.answer(format.format_signal(sig), reply_markup=order_keyboard(sig.symbol, sig.base))
    await message.answer("⬇️ Продолжить:", reply_markup=main_menu_keyboard())


def _normalize_symbol(raw: str) -> str:
    s = raw.upper().strip().replace("/", "").replace("-", "").replace(" ", "")
    if s.endswith("USDT"):
        return s
    if s.endswith("USD"):
        return s[:-3] + "USDT"
    if s.endswith("BTC"):
        return s[:-3] + "BTC"
    return s + "USDT"


def format_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
