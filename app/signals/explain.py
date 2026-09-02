"""
app/signals/explain.py — объяснение сигнала человеческим языком.

Задача: новичок должен за 10 секунд понять «почему эта монета и почему сейчас»,
а опытный трейдер — увидеть конкретные аргументы (структура, зоны, funding).
Текст собирается из фактических наблюдений, а не из шаблонов вида «сильный
восходящий тренд»: каждый пункт привязан к вычисленному значению.
"""

from __future__ import annotations

from typing import List, Optional

from app.analysis.base import MarketFeatures
from app.domain.models import Direction, Signal, TradePlan
from app.scoring.scorer import ScoreResult, confidence_label, potential_label


def detect_setup(features: MarketFeatures, score: ScoreResult) -> str:
    """Короткое имя сетапа — то, что трейдер видит в списке сигналов."""
    long_side = score.direction is Direction.LONG
    compression = features.metrics.get("compression", 0.0)
    has_ob = any(
        (z.is_bullish == long_side) and not z.mitigated
        for st in features.structure.values() for z in st.order_blocks
    )
    has_sweep = bool(features.struct(features.signal_tf) and
                     features.struct(features.signal_tf).sweeps)
    funding = features.derivatives.funding_pct
    rsi = features.primary().rsi

    parts: List[str] = []
    if compression and compression >= 0.55:
        parts.append("сжатие волатильности")
    if has_sweep:
        parts.append("снятие ликвидности")
    if has_ob:
        parts.append(f"{'бычий' if long_side else 'медвежий'} Order Block")
    if funding is not None and abs(funding) >= 0.05:
        parts.append("перегретый funding")
    if rsi == rsi and (rsi < 32 or rsi > 68):
        parts.append("экстремум RSI")
    struct = features.struct(features.signal_tf)
    if struct and struct.last_event and struct.last_event.bars_ago <= 10:
        parts.append("свежий пробой структуры")
    if not parts:
        parts.append("согласование факторов")
    action = "Long" if long_side else "Short"
    return f"{action}: " + " + ".join(parts[:3])


def build_summary(features: MarketFeatures, score: ScoreResult,
                  plan: Optional[TradePlan], max_points: int = 3) -> str:
    """
    Текст «Почему»: 2–3 главных аргумента + главный риск + что это значит.

    Формат — короткие фразы через «•», чтобы читалось и в телефоне.
    """
    direction = score.direction
    if direction is Direction.WAIT:
        return _wait_summary(features, score)

    lines: List[str] = []
    evidence = score.top_evidence(max_points)
    for item in evidence:
        lines.append(f"• {item}")

    # Потенциал движения — ключевая идея продукта.
    potential_text = _potential_phrase(features, score)
    if potential_text:
        lines.append(f"• {potential_text}")

    risks = score.risks(1)
    if risks:
        lines.append(f"⚠️ Против: {risks[0]}")

    # Финальная фраза: что делать и на какой горизонт.
    horizon = plan.horizon_hours if plan else 24
    verb = "рост" if direction is Direction.LONG else "снижение"
    lines.append(
        f"Итого: {confidence_label(score.confidence)} уверенность в том, что "
        f"ближайшее движение — {verb}; горизонт ~{horizon} ч.")
    return "\n".join(lines)


def _potential_phrase(features: MarketFeatures, score: ScoreResult) -> str:
    m = features.metrics
    bits: List[str] = []
    compression = m.get("compression", float("nan"))
    if compression == compression and compression >= 0.5:
        atr_pct = m.get("atr_percentile", float("nan"))
        extra = f" (ATR на {atr_pct:.0f} перцентиле)" if atr_pct == atr_pct else ""
        bits.append(f"волатильность сжата{extra} — пружина взведена")
    change_7d = m.get("change_7d", float("nan"))
    if change_7d == change_7d and abs(change_7d) < 12:
        bits.append(f"за 7 дней {change_7d:+.1f}% — движение ещё впереди")
    oi = m.get("oi_change_pct", float("nan"))
    if oi == oi and oi > 5:
        bits.append(f"открытый интерес +{oi:.0f}% — позиции набирают")
    if not bits:
        return potential_label(score.potential)
    return ", ".join(bits[:2])


def _wait_summary(features: MarketFeatures, score: ScoreResult) -> str:
    """Объяснение, почему сигнала нет — не менее важно, чем сам сигнал."""
    lines: List[str] = []
    if score.notes:
        lines.append(f"• {score.notes[0]}")
    if score.bullish and score.bearish:
        lines.append(f"• Аргументы разделились: {score.bullish[0]}")
        lines.append(f"• Против этого: {score.bearish[0]}")
    elif score.bullish:
        lines.append(f"• Пока только бычьи аргументы: {score.bullish[0]} — подтверждения мало")
    elif score.bearish:
        lines.append(f"• Пока только медвежьи аргументы: {score.bearish[0]} — подтверждения мало")
    else:
        lines.append("• Значимых аргументов нет: рынок в равновесии")
    lines.append("Итого: сделка с таким набором — лотерея. Ждём подтверждения.")
    return "\n".join(lines)


def beginner_note(signal: Signal) -> str:
    """
    Короткая памятка для новичка (по кнопке «Как читать сигнал»).
    Без обещаний прибыли — с напоминанием про риск.
    """
    plan = signal.plan
    rr = plan.rr_primary if plan else 0.0
    direction_ru = {"LONG": "рост цены", "SHORT": "снижение цены",
                    "WAIT": "нет сделки"}[signal.direction.value]
    return (
        "📖 Как читать этот сигнал\n"
        f"• Направление: бот ждёт {direction_ru} на горизонте ~{signal.horizon_hours} ч.\n"
        "• Вход — ЗОНА, а не цена: ставьте лимитный ордер внутри диапазона входа.\n"
        "• Стоп обязателен: он показывает, где идея считается сломанной.\n"
        "• TP1/TP2/TP3 — цели; фиксируйте часть прибыли на каждой.\n"
        f"• R:R {rr:.1f} означает: потенциальная прибыль в {rr:.1f} раза больше риска.\n"
        "• Уверенность — это сила совпадения аргументов, а НЕ вероятность прибыли. "
        "Даже сигнал 9/10 может не сработать.\n"
        "• Риск на сделку — не более 1–2% депозита. Бот не является инвестрекомендацией."
    )
