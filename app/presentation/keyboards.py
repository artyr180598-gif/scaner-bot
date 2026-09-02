"""
app/presentation/keyboards.py — все inline-клавиатуры бота.

Весь интерфейс кнопочный: ни одно действие не требует запоминать команды.
Данные кнопок компактные (лимит Telegram — 64 байта), поэтому символы в
callback передаются базовым тикером, а сигналы — индексом в последнем отчёте.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.models import RiskProfile, ScanReport, Signal, Timeframe
from app.presentation.format import base_of

# ---------------------------------------------------------------------------
# Протокол callback-данных
# ---------------------------------------------------------------------------
CB_MENU = "menu"
CB_SCAN = "scan"
CB_TOP = "top"
CB_MARKET = "market"
CB_HELP = "help"
CB_STATS = "stats"
CB_HEALTH = "health"
CB_SETTINGS = "set"
CB_WATCH = "wl"


def cb_signal(index: int) -> str:
    return f"sig:{index}"


def cb_deep(index: int) -> str:
    return f"deep:{index}"


def cb_coin(base: str) -> str:
    return f"coin:{base[:14].upper()}"


def cb_watch_add(base: str) -> str:
    return f"wl+:{base[:14].upper()}"


def cb_watch_del(base: str) -> str:
    return f"wl-:{base[:14].upper()}"


def cb_watch_clear() -> str:
    return "wl:clear"


def cb_set(key: str, value: str) -> str:
    return f"set:{key}:{value[:20]}"


def _rows(buttons: List[InlineKeyboardButton], per_row: int = 1
          ) -> List[List[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


# ---------------------------------------------------------------------------
# Главное меню
# ---------------------------------------------------------------------------

def main_menu() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("🔍 Найти перспективные монеты", callback_data=CB_SCAN),
        InlineKeyboardButton("📊 Глубокий анализ монеты", callback_data="pick"),
        InlineKeyboardButton("🔥 Топ сигналы сейчас", callback_data=CB_TOP),
        InlineKeyboardButton("📈 Сканер рынка", callback_data=CB_MARKET),
        InlineKeyboardButton("⭐ Мой список", callback_data=CB_WATCH),
        InlineKeyboardButton("📊 Статистика точности", callback_data=CB_STATS),
        InlineKeyboardButton("⚙️ Настройки", callback_data=CB_SETTINGS),
        InlineKeyboardButton("ℹ️ Помощь", callback_data=CB_HELP),
    ]
    return InlineKeyboardMarkup(_rows(buttons, 1))


def menu_row() -> InlineKeyboardMarkup:
    """Компактная строка «в меню» — добавляется к большинству ответов."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 В меню", callback_data=CB_MENU),
        InlineKeyboardButton("🔄 Обновить", callback_data=CB_TOP),
    ]])


# ---------------------------------------------------------------------------
# Сигналы
# ---------------------------------------------------------------------------

def signals_keyboard(signals: Sequence[Signal], per_row: int = 2,
                     max_buttons: int = 14) -> InlineKeyboardMarkup:
    buttons = []
    for i, s in enumerate(signals[:max_buttons]):
        arrow = "🚀" if s.direction.value == "LONG" else "🔻"
        buttons.append(InlineKeyboardButton(
            f"{arrow} ${base_of(s.symbol)} {s.confidence:.1f}",
            callback_data=cb_signal(i),
        ))
    buttons.append(InlineKeyboardButton("📈 Сканер рынка", callback_data=CB_MARKET))
    buttons.append(InlineKeyboardButton("🏠 В меню", callback_data=CB_MENU))
    return InlineKeyboardMarkup(_rows(buttons, per_row))


def signal_actions(symbol: str, *, in_watchlist: bool = False,
                   with_menu: bool = True) -> InlineKeyboardMarkup:
    base = base_of(symbol)
    row1 = [
        InlineKeyboardButton("🔬 Детали анализа", callback_data=cb_coin(base)),
        InlineKeyboardButton(
            "➖ Из списка" if in_watchlist else "⭐ В список",
            callback_data=cb_watch_del(base) if in_watchlist else cb_watch_add(base)),
    ]
    rows = [row1, [
        InlineKeyboardButton("🔄 Обновить", callback_data=cb_coin(base)),
        InlineKeyboardButton("🔗 TradingView",
                             url=f"https://www.tradingview.com/chart/?symbol={base}USDT"),
    ]]
    if with_menu:
        rows.append([
            InlineKeyboardButton("🏠 В меню", callback_data=CB_MENU),
            InlineKeyboardButton("🔥 Топ сигналы", callback_data=CB_TOP),
        ])
    return InlineKeyboardMarkup(rows)


def wait_actions(symbol: str) -> InlineKeyboardMarkup:
    base = base_of(symbol)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔬 Полный разбор", callback_data=cb_coin(base)),
         InlineKeyboardButton("⭐ Следить", callback_data=cb_watch_add(base))],
        [InlineKeyboardButton("🏠 В меню", callback_data=CB_MENU)],
    ])


# ---------------------------------------------------------------------------
# Выбор монеты
# ---------------------------------------------------------------------------

def coin_picker(report: Optional[ScanReport]) -> InlineKeyboardMarkup:
    """Кнопки с монетами из последнего скана + поле ручного ввода."""
    buttons: List[InlineKeyboardButton] = []
    if report is not None:
        seen = set()
        for c in list(report.candidates)[:12]:
            base = base_of(c.symbol)
            if base in seen:
                continue
            seen.add(base)
            comp = c.metrics.get("compression", 0.0)
            buttons.append(InlineKeyboardButton(
                f"${base} · {comp * 100:.0f}%", callback_data=cb_coin(base)))
    for extra in ("BTC", "ETH", "SOL"):
        if extra not in {base_of(b.callback_data.split(":")[-1] + "/USDT") for b in buttons}:
            buttons.append(InlineKeyboardButton(f"${extra}", callback_data=cb_coin(extra)))
    buttons.append(InlineKeyboardButton("🏠 В меню", callback_data=CB_MENU))
    return InlineKeyboardMarkup(_rows(buttons, 3))


def watchlist_keyboard(symbols: Sequence[str]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(f"${base_of(s)}", callback_data=cb_coin(base_of(s)))
               for s in symbols[:18]]
    buttons.append(InlineKeyboardButton("🗑 Очистить список", callback_data=cb_watch_clear()))
    buttons.append(InlineKeyboardButton("➕ Добавить монету", callback_data="pick"))
    buttons.append(InlineKeyboardButton("🏠 В меню", callback_data=CB_MENU))
    return InlineKeyboardMarkup(_rows(buttons, 3))


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def settings_keyboard(current) -> InlineKeyboardMarkup:
    risk_row = [
        InlineKeyboardButton(
            ("✅ " if current.risk_profile == p.value else "") + label,
            callback_data=cb_set("risk", p.value))
        for p, label in ((RiskProfile.CONSERVATIVE, "🛡 Осторожно"),
                         (RiskProfile.MODERATE, "⚖️ Умеренно"),
                         (RiskProfile.AGGRESSIVE, "🔥 Агрессивно"))
    ]
    tf_row = [
        InlineKeyboardButton(
            ("✅ " if current.signal_timeframe == tf.value else "") + label,
            callback_data=cb_set("tf", tf.value))
        for tf, label in ((Timeframe.M15, "15m"), (Timeframe.H1, "1H"),
                          (Timeframe.H4, "4H"))
    ]
    conf_row = [
        InlineKeyboardButton(
            ("✅ " if abs(current.min_confidence - value) < 0.05 else "") + f"{value:g}+",
            callback_data=cb_set("conf", f"{value:g}"))
        for value in (5.0, 6.0, 7.0, 8.0)
    ]
    dep_row = [
        InlineKeyboardButton(
            ("✅ " if abs(current.deposit_usd - value) < 1 else "") + f"${value:g}",
            callback_data=cb_set("dep", f"{value:g}"))
        for value in (100, 500, 1000, 5000, 25000)
    ]
    toggles = [
        InlineKeyboardButton(
            f"📣 Авто-рассылка: {'вкл' if current.auto_push else 'выкл'}",
            callback_data=cb_set("push", "0" if current.auto_push else "1")),
        InlineKeyboardButton(
            f"🎓 Подсказки: {'вкл' if current.show_beginner_hints else 'выкл'}",
            callback_data=cb_set("hints", "0" if current.show_beginner_hints else "1")),
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Риск-профиль", callback_data="noop")],
        risk_row,
        [InlineKeyboardButton("⏱ Таймфрейм сигналов", callback_data="noop")],
        tf_row,
        [InlineKeyboardButton("🔔 Показывать сигналы с уверенностью", callback_data="noop")],
        conf_row,
        [InlineKeyboardButton("💵 Депозит (для расчёта объёма)", callback_data="noop")],
        dep_row,
        toggles,
        [InlineKeyboardButton("🏠 В меню", callback_data=CB_MENU),
         InlineKeyboardButton("🔥 Топ сигналы", callback_data=CB_TOP)],
    ])
