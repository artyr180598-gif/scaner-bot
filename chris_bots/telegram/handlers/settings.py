"""Handlers: настройки (показ текущих параметров)."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

from ..keyboards import back_kb

router = Router(name="settings")


def _get_engine():
    from ...main import app_state
    return app_state["engine"]


@router.callback_query(lambda c: c.data == "settings:menu")
async def show_settings(call: CallbackQuery) -> None:
    if not call.message:
        return
    s = _get_engine().s
    text = (
        "⚙️ <b>Настройки Крис</b>\n\n"
        f"• Биржи: <code>{', '.join(s.exchanges)}</code>\n"
        f"• Quote: <code>{', '.join(s.quote_currencies)}</code>\n"
        f"• Базовый ТФ: <code>{s.base_timeframe}</code>\n"
        f"• ТФ анализа: <code>{', '.join(s.analysis_timeframes)}</code>\n"
        f"• Мин. уверенность: <code>{s.min_confidence:.0f}%</code>\n"
        f"• Мин. объём 24h: <code>${s.min_volume_usd_24h:,.0f}</code>\n"
        f"• Мин/Max ATR%: <code>{s.min_atr_pct:.2f} – {s.max_atr_pct:.2f}</code>\n"
        f"• Анти-гонка: <code>±{s.anti_chase_max_pct:.1f}% за {s.anti_chase_window_bars} баров</code>\n"
        f"• LLM: <code>{'включён (' + s.llm_provider + '/' + s.llm_model + ')' if s.llm_enabled else 'выключен (шаблон)'}</code>\n"
        f"• DRY-RUN: <code>{'да' if s.dry_run else 'нет'}</code>\n\n"
        "<i>Изменить можно через переменные окружения (см. .env.example).</i>"
    )
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()
