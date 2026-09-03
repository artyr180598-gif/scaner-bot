"""
Runtime-контейнер зависимостей (engine, gateway, store, settings).

Отдельный модуль, чтобы любой импорт давал один и тот же app_state,
независимо от того, как запущен бот (python -m crypto_advisor.main и т.п.).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from .scanner.engine import ScannerEngine

log = logging.getLogger(__name__)

app_state: Dict[str, Any] = {}


def set_dependencies(**deps: Any) -> None:
    app_state.clear()
    app_state.update(deps)
    log.debug("runtime dependencies ready: %s", ", ".join(sorted(deps)))


def _require(key: str) -> Any:
    val = app_state.get(key)
    if val is None:
        raise RuntimeError(
            f"{key} не инициализирован: runtime-контейнер пуст. "
            "Бот должен быть запущен через `python -m crypto_advisor.main`."
        )
    return val


def get_engine() -> "ScannerEngine":
    return _require("engine")


def get_settings():
    return _require("settings")


def get_gateway():
    return _require("gateway")


def get_store():
    return _require("store")
