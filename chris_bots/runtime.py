"""
Runtime-контейнер зависимостей (engine, gateway, bus, store, settings).

Почему отдельный модуль, а не main.py:

При запуске `python -m chris_bots.main` файл main.py исполняется как
`__main__` и НЕ регистрируется в sys.modules под именем `chris_bots.main`.
Поэтому late-import `from .main import app_state` из хендлеров загружал
main.py ВТОРОЙ раз — уже как обычный модуль `chris_bots.main`, с собственным
ПУСТЫМ app_state. Хендлеры падали с `KeyError: 'engine'` ещё до первого
`edit_text`/`call.answer()`: кнопки «⚡ Быстрый скан» и «🔎 Глубокий скан»
(а также «⚙️ Настройки» и ввод тикера в «🔬 Глубокий анализ») молча ничего
не делали — у пользователя кнопка просто «крутилась».

Этот модуль никогда не исполняется как `__main__`, поэтому любой импорт
(из main.py, из хендлеров, из тестов) даёт один и тот же объект app_state
внутри одного процесса — как бы ни был запущен бот.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:  # только для типизации, без циклических импортов
    from .scanner import ScannerEngine

log = logging.getLogger(__name__)

# Единый на процесс контейнер зависимостей для хендлеров Telegram.
app_state: Dict[str, Any] = {}


def set_dependencies(**deps: Any) -> None:
    """Наполнить контейнер. Вызывается из main.py при старте, до polling."""
    app_state.clear()
    app_state.update(deps)
    log.debug("runtime dependencies ready: %s", ", ".join(sorted(deps)))


def get_engine() -> "ScannerEngine":
    """
    Движок сканера из контейнера.

    В отличие от `app_state["engine"]` бросает RuntimeError с понятным
    объяснением вместо голого KeyError, если контейнер ещё не заполнен
    (хендлер вызван вне запущенного main.py).
    """
    engine = app_state.get("engine")
    if engine is None:
        raise RuntimeError(
            "ScannerEngine не инициализирован: runtime-контейнер пуст. "
            "Бот должен быть запущен через `python -m chris_bots.main`."
        )
    return engine
