"""
main.py — точка входа. Фоновый Worker-процесс для Railway.

Отвечает за:
  * настройку логирования (UTC, читаемые таймстемпы для панели Railway);
  * загрузку .env локально (на Railway переменные приходят из панели Variables);
  * запуск сканера и его автоматический перезапуск с backoff при падении
    (Railway стартует контейнер один раз, поэтому бесконечный цикл здесь);
  * graceful shutdown по SIGINT/SIGTERM (Railway шлёт SIGTERM при redeploy).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

try:  # необязательная зависимость: нужна только для локальной разработки
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - на Railway dotenv установлен
    pass

from config import Settings
from scanner import ArbitrageScanner
from telegram_bot import TelegramNotifier

APP_NAME = "Arbitrage Scanner (Spot/Futures Hedge)"
APP_VERSION = "2.0.0"

log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

class _UtcFormatter(logging.Formatter):
    """Логи в UTC — стыкуется с таймстемпами Railway."""

    converter = time.gmtime


def setup_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_UtcFormatter(
        "%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.handlers.clear()
    root.addHandler(handler)
    # ccxt слишком болтлив на INFO — оставляем только предупреждения и выше.
    logging.getLogger("ccxt").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Запуск сканера с обработкой сигналов остановки
# ---------------------------------------------------------------------------

async def run_scanner_once(settings: Settings) -> bool:
    """
    Прогон одного жизненного цикла сканера.

    Возвращает True, если процесс попросили остановиться сигналом
    (SIGINT/SIGTERM) — в этом случае супервизор завершает работу.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover — не Linux
            pass

    notifier = TelegramNotifier(
        settings.telegram_bot_token,
        settings.chat_ids,
    )
    scanner = ArbitrageScanner(settings, notifier)
    runner = asyncio.create_task(scanner.run(), name="scanner")
    stopper = asyncio.create_task(stop_event.wait(), name="signal-watcher")

    done, _ = await asyncio.wait(
        {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
    )

    if stopper in done:
        # Railay прислал SIGTERM: аккуратно останавливаем сканер и выходим.
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        stopper.cancel()
        log.info("Процесс остановлен по сигналу — завершаюсь")
        return True

    stopper.cancel()
    # Сканер завершился сам (например, все биржи недоступны) или упал —
    # исключение уйдёт супервизору, который перезапустит его с backoff.
    await runner
    return False


def main() -> int:
    setup_logging("INFO")
    log.info("%s v%s — запуск", APP_NAME, APP_VERSION)

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        return 2
    setup_logging(settings.log_level)
    log.info("%s", settings.describe())

    while True:
        try:
            stopped_by_signal = asyncio.run(run_scanner_once(settings))
            if stopped_by_signal:
                return 0
            log.warning(
                "Сканер завершился неожиданно — перезапуск через %.0fс",
                settings.restart_backoff_seconds,
            )
        except KeyboardInterrupt:
            log.info("Остановлено пользователем (Ctrl+C)")
            return 0
        except Exception:  # noqa: BLE001 — супервизор не должен умирать
            log.exception(
                "Критическая ошибка сканера — перезапуск через %.0fс",
                settings.restart_backoff_seconds,
            )
        time.sleep(settings.restart_backoff_seconds)


if __name__ == "__main__":
    sys.exit(main())
