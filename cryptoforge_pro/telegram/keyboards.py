"""Inline keyboards (aiogram 3.x)."""

from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

RISK_PROFILES = {
    "conservative": "🛡️ Консервативный",
    "balanced": "⚖️ Сбалансированный",
    "aggressive": "🚀 Агрессивный",
}


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Лучшие сетапы сейчас", callback_data="scan_best")],
            [InlineKeyboardButton(text="📈 Только Лонги", callback_data="scan_long")],
            [InlineKeyboardButton(text="📉 Только Шорты", callback_data="scan_short")],
            [InlineKeyboardButton(text="⚡ Скальп (15м–1ч)", callback_data="scan_scalp")],
            [InlineKeyboardButton(text="🎯 Свинг (4ч–Дейли)", callback_data="scan_swing")],
            [InlineKeyboardButton(text="🔍 Поиск по монете / условию", callback_data="search")],
            [InlineKeyboardButton(text="📊 Глубокий анализ монеты", callback_data="analyze")],
            [InlineKeyboardButton(text="⚙️ Настройки риска", callback_data="settings")],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
        ]
    )


def back_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")]]
    )


def search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Глубокий анализ монеты", callback_data="analyze")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")],
        ]
    )


def settings_keyboard(current: Optional[str] = "balanced") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, label in RISK_PROFILES.items():
        mark = "✅" if key == current else ""
        rows.append([InlineKeyboardButton(text=f"{label} {mark}", callback_data=f"profile:{key}")])
    rows.append([InlineKeyboardButton(text="🎚 Установить порог уверенности", callback_data="settings_confidence")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_keyboard(symbol: str, base: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Глубокий анализ", callback_data=f"analyze:{symbol}")],
            [
                InlineKeyboardButton(text="🟢 Идея LONG", callback_data=f"quick:long:{symbol}"),
                InlineKeyboardButton(text="🔴 Идея SHORT", callback_data=f"quick:short:{symbol}"),
            ],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")],
        ]
    )


def result_keyboard(actions: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for a in actions:
        rows.append(
            [InlineKeyboardButton(text=f"🔍 {a.replace('USDT','')} / USDT", callback_data=f"analyze:{a}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Настройки риска", callback_data="settings")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")],
        ]
    )
