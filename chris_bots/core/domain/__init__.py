"""Доменные модели: чистые dataclass'ы без зависимостей от бирж/Telegram."""
from .candle import Candle, Candles
from .market import MarketSnapshot, TickerMeta
from .signal import (
    Direction,
    Signal,
    SignalPlan,
    TakeProfit,
    StopLoss,
    Confidences,
    SignalStatus,
)
from .ticker import Ticker

__all__ = [
    "Candle",
    "Candles",
    "MarketSnapshot",
    "TickerMeta",
    "Direction",
    "Signal",
    "SignalPlan",
    "TakeProfit",
    "StopLoss",
    "Confidences",
    "SignalStatus",
    "Ticker",
]
