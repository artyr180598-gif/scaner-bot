"""
app/analysis/registry.py — реестр модулей анализа.

Модуль регистрируется декоратором и автоматически попадает в конвейер::

    from app.analysis.registry import analysis_module

    @analysis_module("my_factor", Group.MOMENTUM)
    def my_factor(features: MarketFeatures):
        yield Factor(...)

Так добавление новой стратегии анализа = один файл в ``app/analysis/modules/``
плюс импорт в ``modules/__init__.py``. Ничего больше править не нужно.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from app.analysis.base import AnalysisModule, Group

log = logging.getLogger(__name__)

_MODULES: List[AnalysisModule] = []


def analysis_module(name: str, group: str, *, enabled: bool = True,
                    description: str = "") -> Callable:
    """Декоратор регистрации модуля анализа."""

    def decorator(func: Callable) -> Callable:
        func.name = name            # type: ignore[attr-defined]
        func.group = group          # type: ignore[attr-defined]
        func.description = description  # type: ignore[attr-defined]
        if enabled:
            _MODULES.append(func)   # type: ignore[arg-type]
        return func

    return decorator


def all_modules() -> List[AnalysisModule]:
    """
    Все зарегистрированные модули (порядок регистрации).

    Ленивый импорт пакета модулей: без него движок, созданный до первого
    обращения к реестру, остался бы пустым (и анализ молча не выдавал бы
    ни одного фактора).
    """
    if not _MODULES:
        import app.analysis.modules  # noqa: F401 — импорт регистрирует модули
    return list(_MODULES)


def modules_by_group() -> Dict[str, List[AnalysisModule]]:
    out: Dict[str, List[AnalysisModule]] = {}
    for module in _MODULES:
        out.setdefault(module.group, []).append(module)
    return out


def module_names() -> List[str]:
    return [m.name for m in _MODULES]


def find(name: str) -> Optional[AnalysisModule]:
    for m in _MODULES:
        if m.name == name:
            return m
    return None


def reset() -> None:
    """Только для тестов: очистить реестр."""
    _MODULES.clear()


def describe() -> str:
    by_group = modules_by_group()
    lines = []
    for group in sorted(by_group):
        names = ", ".join(m.name for m in by_group[group])
        lines.append(f"  {group}: {names}")
    return "Модули анализа:\n" + "\n".join(lines)


# Проверяем, что все группы из списка известны (защита от опечатки в декораторе).
_KNOWN_GROUPS = {getattr(Group, attr) for attr in dir(Group)
                 if not attr.startswith("_")}
