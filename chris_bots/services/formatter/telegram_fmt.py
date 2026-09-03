"""
Красивые сообщения для Telegram.

Шаблон (по ТЗ):
  🪙 Монета: $TICKER (Long/Short)
  🎯 Уверенность модели: [>75%]
  🧠 Логика входа: ...
  📍 Вход: [зона]
  🏁 Цели (Take Profit): 1) ... 2) ...
  🛑 Отмена сценария (Stop-Loss): [уровень]
"""
from __future__ import annotations

from typing import Iterable

from ...core.domain.signal import Direction, Signal, SignalStatus

HEADER = (
    "🪙 <b>Монета:</b> ${ticker} ({side})\n"
    "🎯 <b>Уверенность модели:</b> {confidence}%\n"
    "🧠 <b>Логика входа:</b>\n{logic}\n"
    "📍 <b>Вход:</b> {entry}\n"
    "🏁 <b>Цели (Take Profit):</b>\n{tps}\n"
    "🛑 <b>Отмена сценария (Stop-Loss):</b> {sl}\n"
    "<i>{disclaimer}</i>"
)

DISCLAIMER = (
    "Сигнал — не финансовая рекомендация. Решение всегда за тобой. "
    "Используй управление рисками."
)


def _ticker_short(symbol: str) -> str:
    """BTC/USDT → BTC."""
    return symbol.split("/")[0] if "/" in symbol else symbol


def format_signal(signal: Signal) -> str:
    """Возвращает HTML-сообщение для Telegram."""
    side = signal.direction.value
    ticker = _ticker_short(signal.symbol)
    confidence = f"{signal.confidences.signal:.1f}"

    entry_low, entry_high = signal.plan.entry_zone
    entry_str = f"{entry_low:.4f} – {entry_high:.4f} (центр {signal.plan.entry_mid:.4f})"

    tps = signal.plan.take_profits
    tp_lines = []
    for tp in tps:
        tp_lines.append(
            f"  {tp.level}) <code>{tp.price:.4f}</code>  ({tp.pct_from_entry:+.2f}%)"
        )
    tps_str = "\n".join(tp_lines) if tp_lines else "—"

    sl = signal.plan.stop_loss
    sl_str = (
        f"<code>{sl.price:.4f}</code> ({sl.pct_from_entry:.2f}%)"
        if sl else "—"
    )

    logic = signal.entry_logic or "Логика не сформирована."

    return HEADER.format(
        ticker=ticker,
        side=side,
        confidence=confidence,
        logic=logic,
        entry=entry_str,
        tps=tps_str,
        sl=sl_str,
        disclaimer=DISCLAIMER,
    )


def format_scan_summary(
    scanned: int,
    candidates: int,
    duration: float,
    signals: Iterable[Signal] = (),
) -> str:
    """Короткий итог скана для Telegram."""
    lines = [
        f"📊 <b>Скан рынка завершён</b>",
        f"Просмотрено: <code>{scanned}</code>",
        f"Кандидатов после фильтров: <code>{candidates}</code>",
        f"Время: <code>{duration:.1f}s</code>",
    ]
    sigs = list(signals)
    if sigs:
        lines.append("\n<b>Топ сигналов:</b>")
        for s in sigs[:5]:
            t = _ticker_short(s.symbol)
            lines.append(
                f"  • <code>{t}</code> · {s.direction.value} · "
                f"уверенность <b>{s.confidences.signal:.0f}%</b>"
            )
    else:
        lines.append("\n<i>Сигналов с уверенностью ≥ 75% не нашёл. Рынок «болото».</i>")
    return "\n".join(lines)
