"""Обработчики: настройки (показ текущих параметров)."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

from ..keyboards import back_kb

router = Router(name="settings")


@router.callback_query(lambda c: c.data == "settings:menu")
async def show_settings(call: CallbackQuery) -> None:
    if not call.message:
        return
    from ...runtime import get_settings
    s = get_settings()
    text = (
        "⚙️ <b>Настройки советника</b>\n\n"
        f"• Биржи: <code>{', '.join(s.exchanges)}</code>\n"
        f"• Quote: <code>{', '.join(s.quote_currencies)}</code>\n"
        f"• Базовый ТФ: <code>{s.base_timeframe}</code>\n"
        f"• ТФ анализа: <code>{', '.join(s.analysis_timeframes)}</code>\n"
        f"• Топ монет для скана: <code>{s.top_n_symbols}</code>\n"
        f"• Мин. объём 24h: <code>${s.min_volume_usd_24h:,.0f}</code>\n"
        f"• Мин/Max ATR%: <code>{s.min_atr_pct:.2f} – {s.max_atr_pct:.2f}</code>\n"
        f"• Анти-гонка: <code>±{s.anti_chase_max_pct:.1f}% за {s.anti_chase_window_bars} баров</code>\n"
        f"• LLM: <code>{'включён (' + s.llm_provider + '/' + s.llm_model + ')' if s.llm_enabled else 'выключен (шаблон)'}</code>\n"
        f"• DRY-RUN: <code>{'да' if s.dry_run else 'нет'}</code>\n\n"
        "<i>Изменения — через переменные окружения (см. .env.example).</i>"
    )
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()


@router.callback_query(lambda c: c.data == "top:recent")
async def show_recent(call: CallbackQuery) -> None:
    """Показывает последние выданные советы из базы."""
    if not call.message:
        return
    from ...runtime import get_store
    store = get_store()
    rows = await store.recent(limit=8)
    if not rows:
        await call.message.edit_text("📭 Пока нет сохранённых советов. Запусти подбор.",
                                     reply_markup=back_kb())
        await call.answer()
        return
    lines = ["📋 <b>Последние советы:</b>"]
    for r in rows:
        conf = r.get("confidence") or 0
        lines.append(f"  • <code>{r['symbol']}</code> · {r['direction']} · {conf:.0f}%")
    await call.message.edit_text("\n".join(lines), reply_markup=back_kb())
    await call.answer()
