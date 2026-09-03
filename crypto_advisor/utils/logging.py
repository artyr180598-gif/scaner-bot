"""Настройка логирования."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Единая настройка логов: время | уровень | модуль | сообщение."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Избегаем дублирующих handler'ов при повторном вызове.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
