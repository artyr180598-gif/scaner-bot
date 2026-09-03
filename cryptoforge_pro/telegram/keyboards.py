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
            [InlineKeyboardButton(text="📊 Обзор рынка", callback_data="market_overview")],
            [InlineKeyboardButton(text="🔍 Поиск по монете / условию", callback_data="search")],
            [InlineKeyboardButton(text="📊 Глубокий анализ монеты", callback_data="analyze")],
            [InlineKeyboardButton(text="📰 Новости рынка", callback_data="news")],
            [InlineKeyboardButton(text="🔔 Ценовые алерты", callback_data="alerts")],
            [InlineKeyboardButton(text="📚 История идей", callback_data="history")],
            [InlineKeyboardButton(text="🧮 Риск-калькулятор", callback_data="risk_calc")],
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


def alerts_keyboard(alerts: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for alert in alerts:
        condition = []
        if alert.get("target_above"):
            condition.append(f"> ${alert.get('target_above'):,.4g}")
        if alert.get("target_below"):
            condition.append(f"< ${alert.get('target_below'):,.4g}")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {alert['symbol']} {' '.join(condition)}",
                    callback_data=f"alert_del:{alert['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить алерт", callback_data="alert_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def future_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")]
        ]
    )
