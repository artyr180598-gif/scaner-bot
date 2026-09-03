"""
Офлайн self-test: прогоняет весь пайплайн на синтетической бирже.

Не требует Telegram-токена и сети. Проверяет:
- парсинг запроса из текста;
- подбор монет (Matcher);
- построение сигнала (Planner + Explainer);
- форматирование для Telegram.

Запуск:
    python -m crypto_advisor.selftest
"""
from __future__ import annotations

import asyncio
import logging
import sys

from .config.settings import Settings
from .core.domain.query import UserRequest
from .data.exchange import get_synthetic
from .scanner.engine import ScannerEngine
from .services.formatter import format_match_list, format_signal
from .utils.logging import setup_logging

log = logging.getLogger(__name__)


async def run() -> int:
    setup_logging("INFO")
    settings = Settings(telegram_token="000000000:TESTTOKENPLACEHOLDERPLACEHOLDER00",
                        exchanges=[], dry_run=True)
    gw = get_synthetic("synthetic")
    engine = ScannerEngine(settings=settings, gateway=gw)

    # 1) Свободный запрос
    request = UserRequest.from_text("агрессивный лонг на 1h, волатильность до 8%")
    print("=== Запрос (парсер) ===")
    print("  ", request.summary)
    print("  Направление:", request.wants_direction.value if request.wants_direction else "авто")

    # 2) Подбор монет
    print("\n=== Подбор монет ===")
    result = await engine.find_matches(request, top_n=30)
    print(f"  Просмотрено: {result.scanned}, отсеяно: {result.rejected}, "
          f"подходящих: {len(result.top)}")
    print(format_match_list(request, result.matches))
    print()

    # 3) Полный совет по лучшей монете
    top = result.top[:3]
    for m in top:
        signal = await engine.analyze_symbol(m.exchange, m.symbol, request)
        if signal is None:
            print(f"  {m.symbol}: сетапа нет")
            continue
        print(f"=== Совет: {m.symbol} ({signal.direction.value}) ===")
        print(format_signal(signal))
        print()

    await engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
