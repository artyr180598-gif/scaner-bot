"""
app/utils/logging.py — единая настройка логирования.

Формат с UTC-таймстемпами (удобно сопоставлять с логами Railway/Docker).
"""

from __future__ import annotations

import logging
import sys
import time


class UtcFormatter(logging.Formatter):
    converter = time.gmtime


DEFAULT_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", stream=None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(UtcFormatter(DEFAULT_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.handlers.clear()
    root.addHandler(handler)
    # Библиотеки-источники шума.
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
