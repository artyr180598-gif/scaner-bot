"""Красивые сообщения для Telegram (HTML)."""

from __future__ import annotations

from typing import List

from ..analysis.matcher import Match
from ..config.profiles import get_profile
from ..core.domain.query import UserRequest
from ..core.domain.signal import Signal


def _ticker_short(symbol: str) -> str:
    return symbol.split("/")[0] if "/" in symbol else symbol


def _price(p: float) -> str:
    """Компактная цена: много знаков для мелких, мало для крупных."""
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.6f}".rstrip("0").rstrip(".")


def _change(c: float) -> str:
    return f"{c:+.2f}%"


def format_signal(signal: Signal) -> str:
    side = signal.direction.value
    ticker = _ticker_short(signal.symbol)
    confidence = f"{signal.confidences.signal:.1f}"

    entry_low, entry_high = signal.plan.entry_zone
    entry_str = f"{_price(entry_low)} – {_price(entry_high)} (центр {_price(signal.plan.entry_mid)})"

    tps = signal.plan.take_profits
    tp_lines = []
    for tp in tps:
        tp_lines.append(f"  {tp.level}) <code>{_price(tp.price)}</code>  ({tp.pct_from_entry:+.2f}%)")
    tps_str = "\n".join(tp_lines) if tp_lines else "—"

    sl = signal.plan.stop_loss
    sl_str = (f"<code>{_price(sl.price)}</code> ({sl.pct_from_entry:.2f}%)"
              if sl else "—")

    reason = signal.reason or "Логика не сформирована."

    change_part = f" · 24ч {_change(signal.change_24h)}" if signal.change_24h else ""
    lines = [
        f"🪙 <b>Монета:</b> ${ticker} ({side})",
        f"🏦 <b>Биржа:</b> {signal.exchange} · <b>Цена:</b> {_price(signal.last_price)}{change_part}",
        f"🎯 <b>Уверенность модели:</b> {confidence}%",
        "🧠 <b>Почему такой сигнал:</b>",
        reason,
        f"📍 <b>Вход:</b> {entry_str}",
        "🏁 <b>Цели (Take Profit):</b>",
        tps_str,
        f"🛑 <b>Отмена сценария (Stop-Loss):</b> {sl_str}",
        "<i>Данные — публичные котировки биржи. Сигнал — согласие технических "
        "факторов, а не финансовая рекомендация. Решение всегда за тобой.</i>",
    ]
    return "\n".join(lines)


def format_match_list(request: UserRequest, matches: List[Match]) -> str:
    """Список подобранных монет под запрос (с реальными котировками)."""
    lines = [
        "🎯 <b>Подобрал монеты по твоему запросу:</b>",
        f"<i>{request.summary}</i>",
        "",
    ]
    top = [m for m in matches if not m.rejected_reason]
    if not top:
        lines.append("🤔 Подходящих монет не нашёл. Измени профиль риска, "
                     "направление или снизь порог уверенности.")
        return "\n".join(lines)

    lines.append("<b>Подходящие монеты (реальные котировки биржи):</b>")
    for i, m in enumerate(top[:8], 1):
        lines.append(
            f"{i}) <code>{m.symbol}</code> · {m.direction.value} · "
            f"цена <b>{_price(m.last_price)}</b> · 24ч <b>{_change(m.change_24h)}</b> · "
            f"уверенность <b>{m.signal_confidence:.0f}%</b>"
        )
    lines.append("\nНажми на монету ниже, чтобы получить полный совет "
                 "(куда входить, цели, стоп и почему).")
    return "\n".join(lines)


def format_matches_buttons_hint() -> str:
    return "⬆️ Выбери монету из списка выше кнопкой."


def format_scan_summary(result, matches: List[Match]) -> str:
    scanned = result.scanned
    top = [m for m in matches if not m.rejected_reason]
    lines = [
        "📊 <b>Скан по запросу завершён</b>",
        f"Просмотрено монет: <code>{scanned}</code>",
        f"Отсеяно фильтрами: <code>{result.rejected}</code>",
        f"Подходящих: <code>{len(top)}</code>",
        f"Время: <code>{result.duration_seconds:.1f}с</code>",
        "<i>Данные — публичные котировки биржи.</i>",
    ]
    return "\n".join(lines)


def format_profile_line(profile_id: str) -> str:
    p = get_profile(profile_id)
    return f"{p.emoji} <b>{p.label}</b> — {p.description}"
