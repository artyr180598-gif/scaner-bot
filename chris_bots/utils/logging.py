"""Настройка логирования."""
from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Одноразовая настройка. Идемпотентна."""
    root = logging.getLogger()
    if root.handlers:
        # Уже настроено.
        for h in root.handlers:
            h.setLevel(getattr(logging, level.upper(), logging.INFO))
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # Глушим слишком шумные библиотеки.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)
