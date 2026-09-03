"""Стратегии (Freqtrade-style IStrategy)."""

from .base import IStrategy
from .momentum_trend import MomentumTrendStrategy
from .registry import get_strategy, register, strategy_names

__all__ = [
    "IStrategy",
    "MomentumTrendStrategy",
    "get_strategy",
    "register",
    "strategy_names",
]
