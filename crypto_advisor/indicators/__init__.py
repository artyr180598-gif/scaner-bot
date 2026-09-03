"""
Реестр индикаторов.

Идея из OctoBot + Freqtrade:
- Каждый индикатор — dataclass с .name, .group, .compute(df) -> df.
- Группы: trend, momentum, volatility, volume, structure, patterns.
- Сигналы получаются как Series (-1, 0, +1) в колонке `<group>_signal`.
"""
from .base import Indicator, SignalSeries, group_signal, attach_signals
from . import trend, momentum, volatility, volume, structure, patterns  # noqa: F401

ALL_GROUPS = ("trend", "momentum", "volatility", "volume", "structure", "patterns")


def all_indicators() -> list[Indicator]:
    """Возвращает все зарегистрированные индикаторы (лениво)."""
    out: list[Indicator] = []
    out += trend.REGISTRY
    out += momentum.REGISTRY
    out += volatility.REGISTRY
    out += volume.REGISTRY
    out += structure.REGISTRY
    out += patterns.REGISTRY
    return out


__all__ = [
    "Indicator",
    "SignalSeries",
    "group_signal",
    "attach_signals",
    "ALL_GROUPS",
    "all_indicators",
]
