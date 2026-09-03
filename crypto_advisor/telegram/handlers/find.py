"""
Обработчики: подбор монет по запросу + глубокий анализ выбранной монеты.

Ключевая фича бота:
1) Пользователь собирает запрос кнопками (профиль риска, направление, ТФ)
   или печатает свой текст.
2) Бот сканирует рынок, подбирает монеты под запрос (Matcher) и ранжирует.
3) Пользователь жмёт на монету → получает полный совет (Лонг/Шорт,
   причина, уверенность, вход/цели/стоп).
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...config.profiles import get_profile, human_label
from ...core.domain.query import UserRequest
from ...core.domain.signal import Direction
from ...services.formatter import format_match_list, format_signal
from ...scanner.engine import ScannerEngine
from ..keyboards import back_kb, find_menu_kb, matches_kb, single_back_kb

log = logging.getLogger(__name__)
router = Router(name="find")

# Текущий «черновик» запроса по chat_id.
_session: dict = {}
# Кеш последних совпадений (для кнопок «match:...»).
_session_last: dict = {}


class FindState(StatesGroup):
    waiting_text = State()


def _get_engine() -> ScannerEngine:
    from ...runtime import get_engine
    return get_engine()


def _defaults() -> dict:
    return {"profile": "balanced", "direction": Direction.NEUTRAL.value, "timeframe": "1h"}


def _current(chat_id: int) -> dict:
    _session.setdefault(chat_id, _defaults())
    return _session[chat_id]


@router.callback_query(lambda c: c.data == "find:menu")
async def find_menu(call: CallbackQuery) -> None:
    if not call.message:
        return
    cur = _current(call.from_user.id)
    await call.message.edit_text(
        "🎯 <b>Настрой запрос</b> — я подберу монеты под него.\n\n"
        "Выбери профиль риска, направление и таймфрейм, "
        "или напиши свой запрос (например: <i>«агрессивный лонг на 1h, волатильность до 8%»</i>).",
        reply_markup=find_menu_kb(cur),
    )
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("find:profile:"))
async def set_profile(call: CallbackQuery) -> None:
    if not call.message:
        return
    pid = call.data.split(":", 2)[2]
    cur = _current(call.from_user.id)
    cur["profile"] = pid
    await call.message.edit_text("Запрос обновлён. Выбери ещё параметры или запускай подбор.",
                                 reply_markup=find_menu_kb(cur))
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("find:dir:"))
async def set_direction(call: CallbackQuery) -> None:
    if not call.message:
        return
    val = call.data.split(":", 2)[2]
    cur = _current(call.from_user.id)
    cur["direction"] = val
    await call.message.edit_text("Запрос обновлён. Выбери ещё параметры или запускай подбор.",
                                 reply_markup=find_menu_kb(cur))
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("find:tf:"))
async def set_timeframe(call: CallbackQuery) -> None:
    if not call.message:
        return
    val = call.data.split(":", 2)[2]
    cur = _current(call.from_user.id)
    cur["timeframe"] = val
    await call.message.edit_text("Запрос обновлён. Выбери ещё параметры или запускай подбор.",
                                 reply_markup=find_menu_kb(cur))
    await call.answer()


@router.callback_query(lambda c: c.data == "find:custom")
async def custom_request(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message:
        return
    await call.message.edit_text(
        "✍️ Опиши запрос текстом. Примеры:\n"
        "<code>агрессивный лонг SOL на 1h</code>\n"
        "<code>стабильные монеты объём от 10 млн</code>\n"
        "<code>шорт на 4h, волатильность до 6%</code>",
        reply_markup=back_kb("find:menu"),
    )
    await state.set_state(FindState.waiting_text)
    await call.answer()


@router.message(FindState.waiting_text)
async def on_custom_text(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    cur = _current(message.chat.id)
    request = UserRequest.from_text(message.text, profile=cur["profile"])
    # Направление/ТФ из текста перекрывают кнопки (текст — это явный запрос).
    if request.wants_direction is not None:
        cur["direction"] = request.direction.value
    cur["timeframe"] = request.timeframe
    cur["custom_request"] = request
    await message.answer(
        f"Принял: <i>{request.summary}</i>.\n"
        "Нажми «Запустить подбор», или подправь параметры кнопками.",
        reply_markup=find_menu_kb(cur),
    )
    await state.clear()


@router.callback_query(lambda c: c.data == "find:quick")
async def quick_scan(call: CallbackQuery) -> None:
    """Быстрый скан рынка без ручной настройки (сбалансированный профиль)."""
    if not call.message:
        return
    await call.answer()
    request = UserRequest.from_text("сбалансированный", profile="balanced")
    await _do_find(call, request)


@router.callback_query(lambda c: c.data == "find:run")
async def run_find(call: CallbackQuery) -> None:
    if not call.message:
        return
    await call.answer()
    cur = _current(call.from_user.id)
    request = cur.get("custom_request") or UserRequest(
        direction=Direction(cur["direction"]),
        risk_profile=cur["profile"],
        timeframe=cur["timeframe"],
    )
    # Пересобираем с актуальным профилем/ТФ из кнопок.
    request.risk_profile = cur["profile"]
    request.timeframe = cur["timeframe"]
    await _do_find(call, request)


async def _do_find(call: CallbackQuery, request: UserRequest) -> None:
    if not call.message:
        return
    try:
        engine = _get_engine()
    except RuntimeError as exc:
        log.error("engine not available: %s", exc)
        await call.message.edit_text("❌ Движок не инициализирован. Перезапусти бота.",
                                     reply_markup=back_kb())
        return

    profile = get_profile(request.risk_profile)
    await call.message.edit_text(
        f"⏳ {profile.emoji} Подбираю монеты: {request.summary}…\n"
        f"Могу сканировать до {engine.s.top_n_symbols} монет. Обычно это 1-3 минуты.",
    )

    started = time.time()
    try:
        result = await engine.find_matches(request)
    except Exception as exc:  # noqa: BLE001
        log.exception("find failed: %s", exc)
        await call.message.edit_text(
            "❌ Не удалось завершить подбор.\n"
            "Проверь: доступ в интернет и что биржа доступна (сейчас используются "
            "публичные котировки, ключи не нужны). Загляни в логи приложения.",
            reply_markup=back_kb(),
        )
        return
    duration = time.time() - started

    # Если биржа вообще не вернула ни одного тикера — это сеть/биржа, а не «нет монет».
    if result.scanned == 0:
        await call.message.edit_text(
            "⚠️ Биржа вернула пустой список тикеров.\n"
            "Скорее всего нет доступа к бирже (нужен интернет; публичные эндпоинты). "
            "Подожди и попробуй ещё раз, либо смени биржу в настройках.",
            reply_markup=back_kb("find:menu"),
        )
        return

    top = [m for m in result.matches if not m.rejected_reason]
    if not top:
        await call.message.edit_text(
            "🤔 Подходящих монет не нашёл по этим критериям.\n"
            "Попробуй смягчить условия: меньше минимальный объём, другой профиль риска, "
            "авто-направление, или напиши запрос своими словами.",
            reply_markup=back_kb("find:menu"),
        )
        return

    _session_last[call.from_user.id] = result.matches
    profile = get_profile(request.risk_profile)
    text = (
        f"{profile.emoji} <b>Подбор завершён за {duration:.1f}с</b>\n"
        f"Биржа: <code>{result.exchange}</code> · просмотрено монет: "
        f"<code>{result.scanned}</code> · подходящих: <code>{len(top)}</code>\n\n"
    )
    text += format_match_list(request, result.matches)
    await call.message.edit_text(text, reply_markup=matches_kb(result.matches))
    await call.answer()


@router.callback_query(lambda c: c.data == "find:back")
async def back_to_matches(call: CallbackQuery) -> None:
    if not call.message:
        return
    matches = _session_last.get(call.from_user.id, [])
    request = _current(call.from_user.id).get("custom_request") or UserRequest(
        direction=Direction(_current(call.from_user.id)["direction"]),
        risk_profile=_current(call.from_user.id)["profile"],
        timeframe=_current(call.from_user.id)["timeframe"],
    )
    await call.message.edit_text(format_match_list(request, matches),
                                 reply_markup=matches_kb(matches))
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("match:"))
async def analyze_match(call: CallbackQuery) -> None:
    if not call.message:
        return
    symbol = call.data.split(":", 1)[1]
    await call.answer()

    matches = _session_last.get(call.from_user.id, [])
    match = next((m for m in matches if m.symbol == symbol and not m.rejected_reason), None)
    engine = _get_engine()

    if match is not None:
        request = UserRequest(
            direction=match.direction, risk_profile="balanced", timeframe="1h", symbols=[symbol])
        await call.message.answer(
            f"⏳ {match.symbol}: углубляю анализ ({match.direction.value}, "
            f"уверенность {match.signal_confidence:.0f}%)…"
        )
        signal = await engine.analyze_symbol(match.exchange, symbol, request)
    else:
        request = UserRequest(risk_profile="balanced", timeframe="1h", symbols=[symbol])
        await call.message.answer("⏳ Анализирую эту монету…")
        exchange = engine._pick_exchange()
        signal = await engine.analyze_symbol(exchange, symbol, request)

    if signal is None:
        await call.message.answer(
            f"🤔 По {symbol} сейчас сетапа с достаточной уверенностью не вижу — рынок нейтрален.",
            reply_markup=back_kb("find:menu"),
        )
    else:
        await call.message.answer(format_signal(signal), reply_markup=single_back_kb())
