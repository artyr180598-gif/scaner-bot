"""
Событийная шина.

Вдохновлено Nautilus Trader: всё в системе — это Event.
Шина простая (asyncio.Queue + подписчики), но модули изолированы.
"""
from .bus import EventBus
from .events import (
    Event,
    CandlesUpdated,
    TickersUpdated,
    SignalGenerated,
    ScanStarted,
    ScanFinished,
)

__all__ = [
    "Event",
    "EventBus",
    "CandlesUpdated",
    "TickersUpdated",
    "SignalGenerated",
    "ScanStarted",
    "ScanFinished",
]
