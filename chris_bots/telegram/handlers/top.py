"""Handlers: топ сетапов Long/Short из последнего скана."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

from ...core.domain.signal import Direction
from ...services.formatter import format_signal
from ..keyboards import back_kb
from .scan import _LAST_SCAN

router = Router(name="top")


def _get_engine():
    # Runtime-контейнер вместо `from ...main import app_state` — иначе при
    # `python -m chris_bots.main` получали второй, пустой app_state.
    from ...runtime import get_engine
    return get_engine()


@router.callback_query(lambda c: c.data in {"top:long", "top:short"})
async def show_top(call: CallbackQuery) -> None:
    if not call.message:
        return
    target = Direction.LONG if call.data == "top:long" else Direction.SHORT

    reports = _LAST_SCAN.get("reports") or {}
    if not reports:
        await call.message.edit_text(
            "📭 Сначала запусти «Сканировать рынок» — потом здесь появятся топ-сетапы.",
            reply_markup=back_kb(),
        )
        await call.answer()
        return

    sigs = []
    for r in reports.values():
        for s in r.signals:
            if s.direction == target:
                sigs.append(s)
    sigs.sort(key=lambda s: s.confidences.signal, reverse=True)
    sigs = sigs[:5]

    if not sigs:
        await call.message.edit_text(
            f"🤔 {target.value}-сетапов с уверенностью ≥ "
            f"{_get_engine().s.min_confidence:.0f}% пока нет. Запусти скан.",
            reply_markup=back_kb(),
        )
        await call.answer()
        return

    await call.message.edit_text(
        f"🏆 <b>Топ {target.value} сетапов:</b>",
        reply_markup=back_kb(),
    )
    await call.answer()
    for s in sigs:
        try:
            await call.message.answer(format_signal(s))
        except Exception:  # noqa: BLE001
            pass
