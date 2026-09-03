"""Алиас офлайн-проверки: ``python -m chris_bots.selftest`` → ``crypto_advisor.selftest``."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from crypto_advisor.selftest import run  # noqa: E402

__all__ = ["run"]

if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
