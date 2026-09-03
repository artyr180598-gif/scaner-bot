"""
Единый запуск всех тестов.

Запуск: python -m chris_bots.tests.runner
"""
from __future__ import annotations

import sys
import traceback
from typing import Callable, List


def _run_suite(name: str, tests: List[Callable[[], None]]) -> int:
    print(f"\n=== {name} ===")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {t.__name__}")
            traceback.print_exc()
    total = len(tests)
    print(f"--- {name}: {total - failed}/{total} passed ---")
    return failed


def main() -> int:
    from chris_bots.tests import smoke
    from chris_bots.tests import config_test
    from chris_bots.tests import indicators_test
    from chris_bots.tests import scoring_test
    from chris_bots.tests import services_test
    from chris_bots.tests import telegram_test
    from chris_bots.tests import backtest_test

    suites = [
        ("smoke", smoke.TESTS),
        ("config", config_test.TESTS),
        ("indicators", indicators_test.TESTS),
        ("scoring/filters/plan", scoring_test.TESTS),
        ("services", services_test.TESTS),
        ("telegram", telegram_test.TESTS),
        ("backtest", backtest_test.TESTS),
    ]
    total_failed = 0
    total_tests = 0
    for name, tests in suites:
        total_tests += len(tests)
        total_failed += _run_suite(name, tests)
    print(f"\n========== TOTAL: {total_tests - total_failed}/{total_tests} passed ==========")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
