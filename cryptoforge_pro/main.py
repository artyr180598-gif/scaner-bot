"""Entry point: python -m cryptoforge_pro.main"""

from __future__ import annotations

import asyncio
import sys

from cryptoforge_pro.app import run
from cryptoforge_pro.config import get_settings
from cryptoforge_pro.utils import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
