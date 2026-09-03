"""Обработчики: /start и главное меню."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from ..keyboards import main_menu_kb

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    text = (
        "👋 <b>Привет! Я — крипто-советник.</b>\n\n"
        "Я умею:\n"
        "• <b>Подбирать монеты</b> по твоему запросу (профиль риска, "
        "направление, таймфрейм или свой текст).\n"
        "• <b>Анализировать</b> их и давать честный совет: Лонг или Шорт, "
        "куда входить, где цели и стоп, почему и с какой уверенностью.\n\n"
        "Выбирай действие:"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.callback_query(lambda c: c.data == "menu:main")
async def back_to_menu(call: CallbackQuery) -> None:
    if not call.message:
        return
    await call.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
    await call.answer()
