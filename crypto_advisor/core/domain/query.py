"""
Запрос пользователя — критерии, по которым мы подбираем монеты.

Это «сердце» фичи «найди монеты по моему запросу». Пользователь либо
выбирает готовый профиль кнопками, либо печатает свободный текст, который
мы разбираем в UserRequest. Дальше Matcher фильтрует и ранжирует монеты
по совпадению с этим запросом.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .signal import Direction

# Направление: auto = пусть модель решает.
# Для русских слов используем ПОДСТРОКУ (морфология): "агрессив" ловит "агрессивный".
_UP_WORDS = ("лонг", "long", "вверх", "рост", "buy", "бычий", "up")
_DOWN_WORDS = ("шорт", "short", "вниз", "падение", "sell", "медвежий", "down")
_RISK_WORDS = {
    "aggressive": ("агрессив", "рискован", "быстр", "impulse", "volatile", "высоковол"),
    "conservative": ("консерват", "спокойн", "надежн", "стабильн", "conservative", "safe", "низковол"),
    "balanced": ("сбаланс", "умерен", "balanced", "нейтральн"),
}
_TIMEFRAME_RE = re.compile(r"(\d{1,2})\s*(m|h|d|w)\b", re.IGNORECASE)
_VOLUME_RE = re.compile(r"(?:объ[её]м|volume|vol)\D{0,8}(\d+(?:\.\d+)?)\s*(млн|m|million|тыс|k)?", re.IGNORECASE)
_ATR_RE = re.compile(r"(?:волат|atr)\D{0,12}(\d+(?:\.\d+)?)\s*%?", re.IGNORECASE)
_TICKER_RE = re.compile(r"\b[A-Z]{2,10}\b")
# Слово-мусор — не тикер и не ключевое слово.
_STOPWORDS = {
    "usdt", "usdc", "usd", "btcusdt", "крипта", "монет", "монету", "биткоин",
    "совет", "по", "на", "для", "до", "с", "и", "или", "без", "в", "о", "да",
    "агрессив", "консерват", "сбаланс", "умерен", "профиль", "риск",
    "волат", "волатильность", "направление", "монеты", "монету",
    "млн", "млрд", "тыс", "объём", "объем", "объема", "от",
}
# Слова, которые точно не являются темой.
_HINT_WORDS = set([
    "лонг", "шорт", "long", "short", "вверх", "вниз", "рост", "падение",
    "buy", "sell", "бычий", "медвежий", "авто", "автомат",
])


def _hit(text: str, word: str) -> bool:
    """Совпадение слова: для латиницы — по границе слова, для кириллицы — по подстроке."""
    if word[0].isascii():
        return bool(re.search(rf"\b{re.escape(word)}\b", text))
    return word in text


@dataclass(slots=True)
class UserRequest:
    """Критерии подбора монет."""

    direction: Direction = Direction.NEUTRAL  # NEUTRAL = auto (пусть модель решит)
    risk_profile: str = "balanced"            # conservative | balanced | aggressive
    timeframe: str = "1h"
    min_volume_usd_24h: float = 5_000_000.0
    max_atr_pct: float = 12.0
    min_atr_pct: float = 0.3
    # Прямо указанные тикеры (пользователь может попросить конкретные монеты).
    symbols: List[str] = field(default_factory=list)
    # Ключевое слово/сектор (мягкий фильтр: совпадает с именем базового токена).
    keyword: str = ""
    # Сколько монет вернуть после ранжирования.
    limit: int = 8
    # Естественно-языковая формулировка (для логов и объяснения).
    raw: str = ""

    @property
    def wants_direction(self) -> Optional[Direction]:
        """None если не задали направление (auto)."""
        return None if self.direction == Direction.NEUTRAL else self.direction

    @property
    def summary(self) -> str:
        parts = [f"профиль «{self.risk_profile}»", f"ТФ {self.timeframe}"]
        if self.wants_direction:
            parts.append(f"только {self.wants_direction.value}")
        else:
            parts.append("авто-направление")
        if self.keyword:
            parts.append(f"тема «{self.keyword}»")
        if self.symbols:
            parts.append(", ".join(self.symbols))
        return ", ".join(parts)

    # ── Парсинг свободного текста ─────────────────────────────
    @classmethod
    def from_text(cls, raw: str, profile: Optional[str] = None) -> "UserRequest":
        """Разбирает свободный запрос в UserRequest. Лучше не падать."""
        text = (raw or "").strip()
        low = text.lower()

        direction = Direction.NEUTRAL
        for w in _UP_WORDS:
            if _hit(low, w):
                direction = Direction.LONG
                break
        if direction == Direction.NEUTRAL:
            for w in _DOWN_WORDS:
                if _hit(low, w):
                    direction = Direction.SHORT
                    break

        # Профиль: явный аргумент важнее слов в тексте.
        risk = profile or ""
        if risk not in ("conservative", "balanced", "aggressive"):
            risk = "balanced"
            for pid, words in _RISK_WORDS.items():
                if any(_hit(low, w) for w in words):
                    risk = pid
                    break

        timeframe = "1h"
        m = _TIMEFRAME_RE.search(low)
        if m:
            unit = m.group(2)
            name = {"m": "m", "h": "h", "d": "d", "w": "w"}[unit]
            timeframe = f"{m.group(1)}{name}"

        # Объём → min_volume.
        volume = 5_000_000.0
        mv = _VOLUME_RE.search(low)
        if mv:
            num = float(mv.group(1))
            mult_word = (mv.group(2) or "").lower()
            if mult_word in ("млн", "m", "million"):
                volume = num * 1_000_000
            elif mult_word in ("тыс", "k"):
                volume = num * 1_000
            else:
                volume = num  # считаем как доллары
            volume = max(volume, 500_000.0)

        # Волатильность → max_atr_pct.
        max_atr = 12.0
        ma = _ATR_RE.search(low)
        if ma:
            max_atr = float(ma.group(1))

        # Прямые тикеры и ключевое слово.
        symbols: List[str] = []
        keywords: List[str] = []
        for tok in re.findall(_TICKER_RE, text):
            tlo = tok.lower()
            if tlo in _STOPWORDS or "USDT" in tok or "USD" in tok:
                continue
            if len(tok) <= 10:
                symbols.append(tok)
                break  # один символ — достаточно, остальное считаем словом
        # Ключевое слово: оставляем осмысленные слова, не токены, не стопы,
        # не слова-подсказки про направление/риск.
        for tok in re.findall(r"[a-zа-яё]{2,}", low):
            if tok in _STOPWORDS or tok in _HINT_WORDS:
                continue
            if any(_hit(tok, w) for w in _UP_WORDS + _DOWN_WORDS):
                continue
            # Отсекаем токены, внутри которых сидит стоп-/подсказка-слово
            # (например «агрессивный» из-за «агрессив»).
            if any(_hit(tok, w) for w in _STOPWORDS):
                continue
            keywords.append(tok)
        keyword = " ".join(sorted(set(keywords))[:3]).strip()

        return cls(
            direction=direction,
            risk_profile=risk,
            timeframe=timeframe,
            min_volume_usd_24h=volume,
            max_atr_pct=max_atr,
            symbols=symbols,
            keyword=keyword,
            raw=text,
        )
