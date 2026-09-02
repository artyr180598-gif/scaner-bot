"""
app/main.py — точка входа.

Режимы:
    python -m app.main                 # запуск Telegram-бота (long polling)
    python -m app.main --selftest      # офлайн-прогон ядра на синтетике (без сети)
    python -m app.main --analyze BTC   # анализ одной монеты в консоли
    python -m app.main --scan          # полный скан рынка в консоли
    python -m app.main --modules       # список подключённых модулей анализа
    python -m app.main --check         # проверка конфигурации

Бот живёт как worker-процесс (не слушает порт) — так его запускают
Railway/Docker/Procfile.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from app import __app_name__, __version__
from app.config.settings import Settings
from app.utils.logging import setup_logging

log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-advisor",
        description="Telegram-бот советник по крипторынку: поиск монет с "
                    "потенциалом движения и сигналы Long/Short.",
    )
    parser.add_argument("--env", default=None, help="путь к .env файлу")
    parser.add_argument("--selftest", action="store_true",
                        help="офлайн-прогон всего конвейера на синтетическом рынке")
    parser.add_argument("--analyze", metavar="SYMBOL",
                        help="глубокий анализ монеты (например, BTC или SOL/USDT)")
    parser.add_argument("--scan", action="store_true",
                        help="полный скан рынка с выводом отчёта в консоль")
    parser.add_argument("--modules", action="store_true",
                        help="показать зарегистрированные модули анализа")
    parser.add_argument("--check", action="store_true",
                        help="проверить конфигурацию и выйти")
    parser.add_argument("--log-level", default=None,
                        help="DEBUG/INFO/WARNING (перекрывает LOG_LEVEL из .env)")
    parser.add_argument("--limit", type=int, default=None,
                        help="сколько монет анализировать в режиме --scan")
    return parser


# ---------------------------------------------------------------------------
# Режимы
# ---------------------------------------------------------------------------

def cmd_modules() -> int:
    from app.analysis.registry import describe

    print(describe())
    return 0


def cmd_check(settings: Settings) -> int:
    print("✅ Конфигурация корректна")
    print(f"   {settings.describe()}")
    missing = []
    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if missing:
        print(f"⚠️ Не заданы переменные: {', '.join(missing)} — бот не запустится")
        return 2
    return 0


async def cmd_analyze(settings: Settings, symbol: str) -> int:
    from app.data.market import MarketDataService
    from app.presentation import render
    from app.services.scanner import ScannerService

    market = await MarketDataService.create(settings)
    try:
        scanner = ScannerService(settings, market)
        signal = await scanner.analyze_symbol(symbol)
    finally:
        await market.close()
    print(render.render_signal(signal, deposit=settings.deposit_usd, show_disclaimer=True))
    print()
    print(render.render_deep_analysis(signal, deposit=settings.deposit_usd))
    return 0 if signal.actionable else 1


async def cmd_scan(settings: Settings, limit: Optional[int] = None) -> int:
    from app.data.market import MarketDataService
    from app.presentation import render
    from app.services.scanner import ScannerService

    market = await MarketDataService.create(settings)
    try:
        if limit:
            settings.prescreen_candidates = limit
            settings.max_universe = max(settings.max_universe, limit * 4)
        scanner = ScannerService(settings, market)
        report = await scanner.run_scan(progress=lambda m, r: print(
            f"  [{r * 100:3.0f}%] {m}"))
    finally:
        await market.close()

    print()
    print(render.render_top_signals(report, limit=settings.top_n))
    print()
    print(render.render_scanner(report, limit=15))
    return 0


def cmd_selftest() -> int:
    """Офлайн-проверка всего конвейера (без сети и без Telegram)."""
    from tools.selftest import main as selftest_main

    return selftest_main()


# ---------------------------------------------------------------------------
# Основной запуск
# ---------------------------------------------------------------------------

async def run(settings: Settings) -> int:
    from app.telegram.bot import run_bot
    from app.telegram.services import create_services

    services = await create_services(settings)
    try:
        await run_bot(settings, services)
    finally:
        await services.close()
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.modules:
        return cmd_modules()

    try:
        settings = Settings.from_env(args.env or ".env")
    except Exception as exc:  # noqa: BLE001
        setup_logging("INFO")
        log.error("ошибка конфигурации: %s", exc)
        return 2

    setup_logging(args.log_level or settings.log_level)
    log.info("%s v%s", __app_name__, __version__)
    log.info("%s", settings.describe())

    if args.check:
        return cmd_check(settings)
    if args.selftest:
        return cmd_selftest()

    try:
        if args.analyze:
            return asyncio.run(cmd_analyze(settings, args.analyze))
        if args.scan:
            return asyncio.run(cmd_scan(settings, args.limit))
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        log.info("остановлено пользователем")
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("критическая ошибка: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
