"""Типы событий."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(slots=True)
class ScanStarted:
    trigger: str = ""
    exchange: str = ""
    request: str = ""


@dataclass(slots=True)
class MarketScanned:
    exchange: str = ""
    scanned: int = 0
    candidates: int = 0
    signals: int = 0
    duration_seconds: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SignalCreated:
    signal_id: str = ""
    symbol: str = ""
    direction: str = ""
    confidence: float = 0.0
    reason: str = ""


EVENT_TYPES: Dict[str, type] = {
    "scan_started": ScanStarted,
    "market_scanned": MarketScanned,
    "signal_created": SignalCreated,
}
