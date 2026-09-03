"""Обработчики: глубокий анализ конкретной монеты по тикеру."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...core.domain.query import UserRequest
from ...core.domain.ticker import Ticker
from ...scanner.engine import ScannerEngine
from ...services.formatter import format_signal
from ..keyboards import back_kb

log = logging.getLogger(__name__)
router = Router(name="analyze")


class AnalyzeState(StatesGroup):
    waiting_ticker = State()


def _get_engine() -> ScannerEngine:
    from ...runtime import get_engine
    return get_engine()


@router.callback_query(lambda c: c.data == "analyze:prompt")
async def ask_ticker(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message:
        return
    await call.message.edit_text(
        "🔬 Введи тикер для глубокого анализа.\n"
        "Формат: <code>BTC/USDT</code>, <code>BTCUSDT</code> или <code>SOL</code>.\n\n"
        "Примеры: <code>ETH/USDT</code>, <code>SOL/USDT</code>, <code>TON/USDT</code>.",
        reply_markup=back_kb(),
    )
    await state.set_state(AnalyzeState.waiting_ticker)
    await call.answer()


@router.message(AnalyzeState.waiting_ticker)
async def do_analyze(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    raw = message.text.strip()
    try:
        ticker = Ticker.parse(raw)
    except ValueError:
        await message.answer(
            f"Не могу разобрать тикер «{raw}». Пример: <code>BTC/USDT</code>.",
            reply_markup=back_kb(),
        )
        return

    try:
        engine = _get_engine()
    except RuntimeError as exc:
        log.error("analyze: engine not available: %s", exc)
        await message.answer("❌ Движок не инициализирован. Перезапусти бота.",
                             reply_markup=back_kb())
        await state.clear()
        return

    sym = ticker.symbol
    exchange = engine._pick_exchange()
    request = UserRequest(risk_profile="balanced", timeframe="1h", symbols=[ticker.base])

    await message.answer(f"⏳ Анализирую {sym} на {exchange}…")
    signal = await engine.analyze_symbol(exchange, sym, request)
    if signal is None:
        await message.answer(
            f"🤔 По {sym} сейчас сетапа с достаточной уверенностью не вижу — рынок нейтрален.",
            reply_markup=back_kb(),
        )
    else:
        await message.answer(format_signal(signal), reply_markup=back_kb())
    await state.clear()
