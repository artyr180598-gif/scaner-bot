"""
Совместимый алиас пакета ``crypto_advisor``.

Хостинг запускает бота командой::

    python -m chris_bots.main

а пакет в репозитории называется ``crypto_advisor`` (бот был переписан заново).
Чтобы старая команда запуска продолжала работать без правок на сервере, этот
пакет-обёртка просто пробрасывает всё в ``crypto_advisor``.

Канонический запуск: ``python -m crypto_advisor.main``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Гарантируем, что корень репозитория лежит в sys.path, даже если бота
# запускают из другой рабочей директории (например, из systemd/docker
# с рабочим каталогом вне проекта).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: Алиас основного пакета — ``chris_bots.crypto_advisor`` == ``crypto_advisor``.
from crypto_advisor import *  # noqa: E402,F401,F403
from crypto_advisor import main as main_module  # noqa: E402

__all__ = ["main_module", "crypto_advisor"]
__version__ = "1.0.0"
