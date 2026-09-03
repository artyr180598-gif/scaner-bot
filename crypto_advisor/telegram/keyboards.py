"""Inline-клавиатуры Telegram."""

from __future__ import annotations

from typing import List

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..config.profiles import PROFILES
from ..core.domain.signal import Direction


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Подобрать монеты по запросу", callback_data="find:menu")],
            [InlineKeyboardButton(text="⚡ Быстрый скан рынка", callback_data="find:quick")],
            [InlineKeyboardButton(text="📋 Последние советы", callback_data="top:recent")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings:menu")],
        ]
    )


def back_kb(callback: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data=callback)]]
    )


def find_menu_kb(current: dict) -> InlineKeyboardMarkup:
    """Экран настройки запроса: профиль + направление + ТФ + «свой запрос»."""
    # Профиль
    profile_row = []
    for pid in PROFILES.values():
        selected = "✅" if pid.id == current.get("profile") else ""
        profile_row.append(InlineKeyboardButton(
            text=f"{pid.emoji} {selected}{pid.label}", callback_data=f"find:profile:{pid.id}"))
    # Направление
    dir_row = []
    for d in (Direction.NEUTRAL, Direction.LONG, Direction.SHORT):
        label = {"Long": "📈 Лонг", "Short": "📉 Шорт", "Neutral": "🔄 Авто"}[d.value]
        selected = "✅" if current.get("direction") == d.value else ""
        dir_row.append(InlineKeyboardButton(
            text=f"{selected}{label}", callback_data=f"find:dir:{d.value}"))
    # Таймфрейм
    tf_row = []
    for tf in ("15m", "1h", "4h", "1d"):
        selected = "✅" if current.get("timeframe") == tf else ""
        tf_row.append(InlineKeyboardButton(
            text=f"{selected}{tf}", callback_data=f"find:tf:{tf}"))

    return InlineKeyboardMarkup(
        inline_keyboard=[
            profile_row,
            dir_row,
            tf_row,
            [InlineKeyboardButton(text="✍️ Свой запрос", callback_data="find:custom")],
            [InlineKeyboardButton(text="▶ Запустить подбор", callback_data="find:run")],
            [InlineKeyboardButton(text="◀ Назад", callback_data="menu:main")],
        ]
    )


def matches_kb(matches: List) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for m in matches:
        if m.rejected_reason:
            continue
        rows.append([InlineKeyboardButton(
            text=f"🔬 {m.symbol} · {m.direction.value} · {m.signal_confidence:.0f}%",
            callback_data=f"match:{m.symbol}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="find:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def single_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀ К списку монет", callback_data="find:back")]]
    )
