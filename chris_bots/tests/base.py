"""
Базовые утилиты для тестов.
"""
from __future__ import annotations

import traceback
from typing import Callable, Iterable

import numpy as np
import pandas as pd

OK = "[OK]  "
FAIL = "[FAIL]"


def make_synthetic_df(n: int = 200, trend: str = "up", seed: int = 42) -> pd.DataFrame:
    """Генерирует синтетический OHLCV с трендом up/down/sideways."""
    np.random.seed(seed)
    drift = {"up": 0.0008, "down": -0.0008, "sideways": 0.0}.get(trend, 0.0)
    vol = 0.02
    rets = np.random.normal(drift, vol, n)
    prices = 100 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({
        "timestamp": np.arange(n) * 3600_000,
        "open": prices * (1 + np.random.normal(0, 0.001, n)),
        "high": prices * (1 + np.abs(np.random.normal(0, 0.005, n))),
        "low": prices * (1 - np.abs(np.random.normal(0, 0.005, n))),
        "close": prices,
        "volume": np.random.uniform(100, 1000, n),
    })
    return df


def run_test(tests: Iterable[Callable[[], None]], header: str) -> int:
    """Запускает список тестовых функций, печатает результат."""
    print(header)
    failed = 0
    for t in tests:
        try:
            t()
        except Exception:  # noqa: BLE001
            failed += 1
            print(FAIL + f" {t.__name__}")
            traceback.print_exc()
    total = len(list(tests))
    print(f"=== {total - failed}/{total} passed ===\n")
    return failed
