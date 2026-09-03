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
    """Берём движок из workflow_data (см. main.py)."""
    from ...main import app_state  # late import, чтобы не было цикла
    return app_state["engine"]


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

    # Подкручиваем top_n на лету (демо-режим).
    engine: ScannerEngine = _get_engine()
    original = engine.s.top_n_symbols
    engine.s.top_n_symbols = top_n  # type: ignore[misc]

    await call.message.edit_text(
        f"⏳ Запускаю {'глубокий' if deep else 'быстрый'} скан рынка…\n"
        f"Топ монет: {top_n}. Это может занять 1-3 минуты.",
    )
    await call.answer()

    started = time.time()
    try:
        async with _SCAN_LOCK:
            reports = await engine.scan_all()
            _LAST_SCAN["reports"] = reports
            _LAST_SCAN["timestamp"] = time.time()
    finally:
        engine.s.top_n_symbols = original  # type: ignore[misc]

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
