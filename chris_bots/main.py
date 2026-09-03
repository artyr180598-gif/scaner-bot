"""
Точка входа для запуска командой ``python -m chris_bots.main``.

Это тонкая обёртка над ``crypto_advisor.main`` — весь код живёт в
``crypto_advisor/``, здесь только проброс, чтобы старая команда запуска
на хостинге не падала с ``ModuleNotFoundError: No module named 'chris_bots'``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from crypto_advisor.main import main, run  # noqa: E402

__all__ = ["main", "run"]

if __name__ == "__main__":
    sys.exit(run())
