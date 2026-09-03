"""Реестр стратегий (Freqtrade-style: стратегии подключаются как классы)."""

from __future__ import annotations

from typing import Dict

from .base import IStrategy
from .momentum_trend import MomentumTrendStrategy

_REGISTRY: Dict[str, type] = {
    "momentum_trend": MomentumTrendStrategy,
}


def register(cls: type) -> type:
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str = "momentum_trend") -> IStrategy:
    cls = _REGISTRY.get(name) or MomentumTrendStrategy
    return cls()


def strategy_names() -> list[str]:
    return list(_REGISTRY.keys())
