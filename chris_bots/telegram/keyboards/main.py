"""
Inline-клавиатуры.

Кнопки по ТЗ:
- «Сканировать рынок»
- «Глубокий анализ монеты»
- «Топ сетапов (Long/Short)»
- «Настройки»
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Сканировать рынок", callback_data="scan:market")],
            [InlineKeyboardButton(text="🔬 Глубокий анализ монеты", callback_data="analyze:prompt")],
            [InlineKeyboardButton(text="🏆 Топ сетапов (Long)", callback_data="top:long")],
            [InlineKeyboardButton(text="🏆 Топ сетапов (Short)", callback_data="top:short")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings:menu")],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="menu:main")]]
    )


def scan_options_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Быстрый скан (топ-50)", callback_data="scan:fast")],
            [InlineKeyboardButton(text="🔎 Глубокий скан (топ-150)", callback_data="scan:deep")],
            [InlineKeyboardButton(text="◀ Назад", callback_data="menu:main")],
        ]
    )
