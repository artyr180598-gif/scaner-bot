"""
Доменная модель сигнала.

Идея: Signal — это финальный артефакт, который формирует движок и который
форматирует Telegram-бот. Signal Plan включает вход/цели/стоп, Confidences
разделены на две (data + signal), чтобы честно показывать пользователю,
насколько данные полные и насколько модель уверена.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Direction(str, Enum):
    LONG = "Long"
    SHORT = "Short"
    NEUTRAL = "Neutral"  # сценарий не сформирован


class SignalStatus(str, Enum):
    DRAFT = "draft"  # сигнал в работе, ещё не опубликован
    PUBLISHED = "published"  # опубликован в Telegram
    EXPIRED = "expired"  # сценарий отменён по времени
    HIT_TP = "hit_tp"
    HIT_SL = "hit_sl"


@dataclass(slots=True)
class TakeProfit:
    level: int  # 1, 2, 3
    price: float
    pct_from_entry: float  # сколько % от входа


@dataclass(slots=True)
class StopLoss:
    price: float
    pct_from_entry: float  # отрицательный
    rationale: str = ""


@dataclass(slots=True)
class Confidences:
    """
    Двойная уверенность (урок BRAIN.md п.0):
    - data: насколько полные/свежие данные (0..100).
    - signal: насколько факторы согласны (0..100). НЕ равно вероятности profit.
    """

    data: float = 0.0
    signal: float = 0.0
    risk_profile: str = "balanced"  # conservative | balanced | aggressive
    caps: dict = field(default_factory=dict)  # какие группы дали вклад


@dataclass(slots=True)
class SignalPlan:
    """План сделки: вход, цели, стоп."""

    entry_zone: tuple  # (low, high) — диапазон входа
    entry_mid: float  # середина зоны
    take_profits: List[TakeProfit] = field(default_factory=list)
    stop_loss: Optional[StopLoss] = None
    risk_reward: float = 0.0  # RR по TP1
    leverage_suggestion: float = 1.0  # плечо (для фьючерсов)


@dataclass(slots=True)
class Signal:
    """
    Финальный сигнал, который публикуется в Telegram.

    Содержит всё, что нужно для красивого сообщения от Крис.
    """

    # Идентификация
    symbol: str
    exchange: str
    direction: Direction

    # Контекст рынка
    last_price: float
    timeframe_base: str  # базовый ТФ, на котором искали
    timeframes_used: List[str]  # все ТФ, что легли в решение

    # Уверенность
    confidences: Confidences

    # План
    plan: SignalPlan

    # Логика входа (генерируется LLM или шаблоном)
    entry_logic: str
    logic_factors: List[str] = field(default_factory=list)  # какие факторы зажглись

    # Мета
    signal_id: str = ""
    created_at: float = 0.0
    status: SignalStatus = SignalStatus.DRAFT

    # ── Сериализация ───────────────────────────────────────────
    def short_id(self) -> str:
        return self.signal_id[:8] if self.signal_id else f"{self.exchange}:{self.symbol}"

    def min_confidence(self) -> float:
        """Минимальная из двух уверенностей (узкое место)."""
        return min(self.confidences.data, self.confidences.signal)
