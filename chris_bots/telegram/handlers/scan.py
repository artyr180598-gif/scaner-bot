"""Handlers: сканирование рынка."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from ...core.domain.signal import Direction
from ...scanner import ScannerEngine
from ...services.formatter import format_scan_summary, format_signal
from ..keyboards import back_kb, main_menu_kb, scan_options_kb

log = logging.getLogger(__name__)
router = Router(name="scan")

# Глобальный кеш последнего скана (для команд «Топ»).
_LAST_SCAN: dict = {"timestamp": 0.0, "reports": {}}
_SCAN_LOCK = asyncio.Lock()


def _get_engine() -> ScannerEngine:
    """Берём движок из runtime-контейнера (см. chris_bots/runtime.py).

    ВАЖНО: раньше импортировали `from ...main import app_state` — при запуске
    `python -m chris_bots.main` это создавало второй экземпляр main.py с ПУСТЫМ
    app_state, хендлер падал с KeyError до ответа пользователю, и кнопки
    «Быстрый/Глубокий скан» молча ничего не делали.
    """
    from ...runtime import get_engine
    return get_engine()


@router.callback_query(lambda c: c.data == "scan:market")
async def scan_menu(call: CallbackQuery) -> None:
    if not call.message:
        return
    await call.message.edit_text(
        "Какой режим скана запустить?",
        reply_markup=scan_options_kb(),
    )
    await call.answer()


@router.callback_query(lambda c: c.data in {"scan:fast", "scan:deep"})
async def scan_run(call: CallbackQuery) -> None:
    if not call.message:
        return
    deep = call.data == "scan:deep"
    top_n = 150 if deep else 50

    # Отвечаем на нажатие СРАЗУ: что бы ни случилось дальше, кнопка не будет
    # «висеть крутящейся», а пользователь всегда увидит видимый результат.
    await call.answer()

    # Лимит top_n передаём параметром (Settings frozen — его нельзя
    # мутировать на лету, поэтому проброс через параметры, а не правка .env).
    try:
        engine: ScannerEngine = _get_engine()
    except RuntimeError as exc:
        log.error("scan: engine not available: %s", exc)
        await call.message.edit_text(
            "❌ Движок сканера не инициализирован. Перезапусти бота "
            "(<code>python -m chris_bots.main</code>) и попробуй ещё раз.",
            reply_markup=back_kb(),
        )
        return

    if _SCAN_LOCK.locked():
        await call.message.edit_text(
            "⏳ Предыдущий скан ещё идёт — дождись его итогов и запусти новый.",
            reply_markup=back_kb(),
        )
        return

    await call.message.edit_text(
        f"⏳ Запускаю {'глубокий' if deep else 'быстрый'} скан рынка…\n"
        f"Топ монет: {top_n}. Это может занять 1-3 минуты.",
    )

    started = time.time()
    try:
        async with _SCAN_LOCK:
            reports = await engine.scan_all(top_n=top_n)
            _LAST_SCAN["reports"] = reports
            _LAST_SCAN["timestamp"] = time.time()
    except Exception as exc:  # noqa: BLE001
        # Ошибка одного запуска не должна превращаться в необработанный
        # traceback aiogram и оставлять пользователя на экране «запускаю».
        log.exception("market scan failed: %s", exc)
        await call.message.edit_text(
            "❌ Не удалось завершить сканирование. Проверь логи приложения "
            "и доступность биржи, затем попробуй ещё раз.",
            reply_markup=back_kb(),
        )
        return

    duration = time.time() - started
    # Суммируем по биржам.
    total_scanned = sum(r.scanned for r in reports.values())
    total_candidates = sum(r.candidates for r in reports.values())
    all_signals = []
    for r in reports.values():
        all_signals.extend(r.signals)
    # Сортируем по signal confidence.
    all_signals.sort(key=lambda s: s.confidences.signal, reverse=True)

    summary = format_scan_summary(
        total_scanned, total_candidates, duration, all_signals[:5]
    )
    await call.message.edit_text(summary, reply_markup=back_kb())

    # Публикуем лучшие сигналы отдельными сообщениями.
    for s in all_signals[:3]:
        try:
            await call.message.answer(format_signal(s))
        except Exception as exc:  # noqa: BLE001
            log.warning("publish signal failed: %s", exc)
