"""
Базовые события.

Минимальный набор для движка. Расширяется по мере роста.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time
import uuid


@dataclass
class Event:
    """Базовый класс события."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    @property
    def type(self) -> str:
        return self.__class__.__name__


@dataclass
class TickersUpdated(Event):
    """Биржа вернула обновление тикеров."""

    exchange: str = ""
    symbols: List[str] = field(default_factory=list)
    count: int = 0


@dataclass
class CandlesUpdated(Event):
    """Подтянули свечи по символу/ТФ."""

    exchange: str = ""
    symbol: str = ""
    timeframe: str = ""
    count: int = 0


@dataclass
class ScanStarted(Event):
    """Сканер начал работу."""

    trigger: str = "manual"  # manual | scheduled | command
    requested_by: int = 0  # chat_id


@dataclass
class ScanFinished(Event):
    """Сканер закончил. signals — список коротких превью."""

    duration_seconds: float = 0.0
    scanned: int = 0
    candidates: int = 0
    signals: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SignalGenerated(Event):
    """Сформирован новый сигнал."""

    signal_id: str = ""
    symbol: str = ""
    exchange: str = ""
    direction: str = ""
    confidence: float = 0.0
    payload: Optional[Dict[str, Any]] = None
