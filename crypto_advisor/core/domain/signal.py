"""Доменная модель сигнала (совет), который публикуется в Telegram."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class Direction(str, Enum):
    LONG = "Long"
    SHORT = "Short"
    NEUTRAL = "Neutral"


class SignalStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    EXPIRED = "expired"


@dataclass(slots=True)
class TakeProfit:
    level: int
    price: float
    pct_from_entry: float


@dataclass(slots=True)
class StopLoss:
    price: float
    pct_from_entry: float
    rationale: str = ""


@dataclass(slots=True)
class Confidences:
    """Двойная уверенность: data (полнота данных) и signal (согласие факторов)."""

    data: float = 0.0
    signal: float = 0.0
    risk_profile: str = "balanced"
    group_caps: Dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class SignalPlan:
    entry_zone: tuple
    entry_mid: float
    take_profits: List[TakeProfit] = field(default_factory=list)
    stop_loss: Optional[StopLoss] = None
    risk_reward: float = 0.0
    leverage_suggestion: float = 1.0


@dataclass(slots=True)
class Signal:
    """Финальный совет: куда входить (Long/Short), почему и с какой уверенностью."""

    symbol: str
    exchange: str
    direction: Direction
    last_price: float
    timeframe_base: str
    timeframes_used: List[str]
    confidences: Confidences
    plan: SignalPlan
    # «Краткое описание, почему такой сигнал» — генерируется Explainer.
    reason: str
    change_24h: float = 0.0
    volume_24h: float = 0.0
    reason_factors: List[str] = field(default_factory=list)
    # Мета
    signal_id: str = ""
    created_at: float = 0.0
    status: SignalStatus = SignalStatus.DRAFT

    def short_id(self) -> str:
        return self.signal_id[:8] if self.signal_id else f"{self.exchange}:{self.symbol}"

    def min_confidence(self) -> float:
        return min(self.confidences.data, self.confidences.signal)
