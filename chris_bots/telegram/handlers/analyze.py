"""Handlers: глубокий анализ конкретной монеты."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...scanner import ScannerEngine
from ...services.formatter import format_signal
from ..keyboards import back_kb

log = logging.getLogger(__name__)
router = Router(name="analyze")


class AnalyzeState(StatesGroup):
    waiting_ticker = State()


def _get_engine() -> ScannerEngine:
    # Runtime-контейнер, а не `from ...main import app_state`: main.py при
    # `python -m` исполняется как __main__, и прямой импорт давал второй,
    # пустой app_state (кнопки молча умирали). См. chris_bots/runtime.py.
    from ...runtime import get_engine
    return get_engine()


@router.callback_query(lambda c: c.data == "analyze:prompt")
async def ask_ticker(call: CallbackQuery, state: FSMContext) -> None:
    if not call.message:
        return
    await call.message.edit_text(
        "🔬 Введи тикер для глубокого анализа.\n"
        "Формат: <code>BTC/USDT</code> или <code>BTCUSDT</code>.\n\n"
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
        from ...core.domain.ticker import Ticker
        ticker = Ticker.parse(raw)
    except ValueError:
        await message.answer(
            f"Не могу разобрать тикер «{raw}». Пример правильного формата: <code>BTC/USDT</code>.",
            reply_markup=back_kb(),
        )
        return

    try:
        engine: ScannerEngine = _get_engine()
    except RuntimeError as exc:
        log.error("analyze: engine not available: %s", exc)
        await message.answer(
            "❌ Движок сканера не инициализирован. Перезапусти бота "
            "(<code>python -m chris_bots.main</code>) и попробуй ещё раз.",
            reply_markup=back_kb(),
        )
        await state.clear()
        return
    sym = ticker.symbol
    exchange = engine.gw.available()[0] if engine.gw.available() else "binance"

    await message.answer(f"⏳ Анализирую {sym} на {exchange}…")

    candles = await engine.gw.fetch_candles(
        exchange, sym, engine.s.base_timeframe, limit=engine.s.candles_limit
    )
    if candles is None or len(candles) < 50:
        await message.answer(
            f"❌ Не нашёл данных по {sym} на {exchange}.",
            reply_markup=back_kb(),
        )
        await state.clear()
        return

    from ...core.domain.market import TickerMeta
    meta = TickerMeta(
        symbol=sym,
        exchange=exchange,
        last_price=float(candles.last.close) if candles.last else 0.0,
        quote_volume_24h=0.0,  # для одиночного анализа не критично
        change_pct_24h=0.0,
    )
    signal = await engine._build_signal_for(meta, candles)  # noqa: SLF001
    if signal is None:
        await message.answer(
            f"🤔 По {sym} сейчас сетапа с уверенностью ≥ "
            f"{engine.s.min_confidence:.0f}% не вижу. Рынок нейтрален.",
            reply_markup=back_kb(),
        )
    else:
        await message.answer(format_signal(signal), reply_markup=back_kb())
    await state.clear()
