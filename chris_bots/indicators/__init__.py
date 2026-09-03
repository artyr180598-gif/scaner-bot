"""
Реестр индикаторов.

Идея из OctoBot + Freqtrade:
- Каждый индикатор — dataclass с .name, .group, .compute(df) -> df.
- Группы: trend, momentum, volatility, volume, structure, patterns.
- Сигналы получаются как Series (-1, 0, +1) в колонке `<name>_signal`.
"""
from .base import Indicator, SignalSeries, group_signal, attach_signals
from . import trend, momentum, volatility, volume, structure, patterns  # noqa: F401

ALL_GROUPS = ("trend", "momentum", "volatility", "volume", "structure", "patterns")


def all_indicators() -> list[Indicator]:
    """Возвращает все зарегистрированные индикаторы (лениво)."""
    from . import trend as _t
    from . import momentum as _m
    from . import volatility as _v
    from . import volume as _vol
    from . import structure as _s
    from . import patterns as _p

    out: list[Indicator] = []
    out += _t.REGISTRY  # type: ignore[attr-defined]
    out += _m.REGISTRY  # type: ignore[attr-defined]
    out += _v.REGISTRY  # type: ignore[attr-defined]
    out += _vol.REGISTRY  # type: ignore[attr-defined]
    out += _s.REGISTRY  # type: ignore[attr-defined]
    out += _p.REGISTRY  # type: ignore[attr-defined]
    return out


__all__ = [
    "Indicator",
    "SignalSeries",
    "group_signal",
    "attach_signals",
    "ALL_GROUPS",
    "all_indicators",
]
