"""Handlers: /start, главное меню."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery

from ...services.formatter import format_signal
from ..keyboards import main_menu_kb

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    text = (
        "👋 <b>Привет! Я Крис</b> — аналитическая модель экосистемы Chris Bets.\n\n"
        "Я ищу крипто-активы на <b>зарождении импульса</b>: ещё не улетевшие, "
        "но уже готовые к движению. Сигнал выдаю только при высокой уверенности "
        "(≥ 75% по шкале модели).\n\n"
        "Выбирай действие:"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.callback_query(lambda c: c.data == "menu:main")
async def back_to_menu(call: CallbackQuery) -> None:
    if not call.message:
        return
    await call.message.edit_text(
        "Главное меню:",
        reply_markup=main_menu_kb(),
    )
    await call.answer()


@router.callback_query(lambda c: c.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await call.answer()
