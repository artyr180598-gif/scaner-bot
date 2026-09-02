"""
app/presentation/format.py — форматирование чисел и времени для интерфейса.

Криптовалюты живут в диапазонах от 0.00000001 до 100 000, поэтому точность
выбирается по порядку величины: BTC показываем как 67 420.5, а PEPE —
как 0.000008123. Иначе половина цифр в сигнале — шум.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional


def fmt_price(value: Optional[float], max_digits: int = 8) -> str:
    """Цена с разумным числом знаков и разделителями тысяч."""
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    value = float(value)
    if value == 0:
        return "0"
    a = abs(value)
    if a >= 10_000:
        digits = 1
    elif a >= 1_000:
        digits = 2
    elif a >= 100:
        digits = 3
    elif a >= 1:
        digits = 4
    elif a >= 0.01:
        digits = 6
    else:
        digits = max_digits          # микро-цены: столько знаков, сколько нужно
    digits = min(digits, max_digits)
    text = f"{value:,.{digits}f}"
    # Убираем незначащие нули в дробной части (0.001200 → 0.0012).
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text.endswith(","):
            text += "0"
    return text or "0"


def fmt_pct(value: Optional[float], digits: int = 2, sign: bool = True) -> str:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    template = f"{{:+.{digits}f}}%" if sign else f"{{:.{digits}f}}%"
    return template.format(value)


def fmt_usd(value: Optional[float], digits: int = 0) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 10_000:
        return f"${value / 1000:.1f}K"
    return f"${value:,.{digits}f}"


def fmt_ratio(value: Optional[float], digits: int = 1) -> str:
    """R:R в виде «1:3.2» — так привычнее трейдеру."""
    if value is None or not math.isfinite(value):
        return "—"
    return f"1:{value:.{digits}f}"


def fmt_confidence(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}/10"


def fmt_number(value: Optional[float], digits: int = 1) -> str:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:,.{digits}f}".replace(",", " ")


def progress_bar(ratio: float, width: int = 10) -> str:
    ratio = max(0.0, min(1.0, float(ratio)))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def gauge(value: float, width: int = 10) -> str:
    """Шкала 0..1 для потенциала/качества данных."""
    return progress_bar(value, width)


def ago(moment: Optional[datetime], now: Optional[datetime] = None) -> str:
    """«5 мин назад», «2 ч назад» — по-русски и без лишних слов."""
    if moment is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds} с назад"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"


def clock(moment: Optional[datetime]) -> str:
    if moment is None:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.strftime("%d.%m %H:%M UTC")


def base_of(symbol: str) -> str:
    """'BTC/USDT' → 'BTC' (для $BTC в тексте сигнала)."""
    return symbol.split("/")[0].upper()


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 монета, 2 монеты, 5 монет."""
    mod10, mod100 = count % 10, count % 100
    if mod10 == 1 and mod100 != 11:
        return one
    if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        return few
    return many
