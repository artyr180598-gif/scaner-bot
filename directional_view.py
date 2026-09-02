"""
directional_view.py — оформление направленных сигналов для Telegram.

Отделено от движка сознательно: движок считает и ничего не знает про HTML,
вью не считает ничего — только раскладывает готовые числа по блокам.

Правила оформления (под чтение с телефона):
  * блоки с эмодзи-заголовками, между блоками — пустая строка;
  * не больше ~25 строк на карточку, детали — по кнопке «Почему»;
  * все числа приходят из движка, вью НИЧЕГО не досчитывает и не округляет
    «в свою пользу»;
  * дисклеймер в каждой карточке сигнала.
"""

from __future__ import annotations

import html
import time
from typing import Any, Iterable, Optional, Sequence

from directional import (
    DEFAULT_CONFIG,
    DirectionalSignal,
    RISK_PROFILES,
    RiskProfile,
    beginner_breakdown,
)

__all__ = [
    "format_signal_card",
    "format_beginner_card",
    "format_why_card",
    "format_top_signals",
    "format_accuracy_card",
    "format_watchlist_card",
    "signal_keyboard",
    "directional_menu_keyboard",
    "profiles_keyboard",
    "DISCLAIMER",
    "BACKTEST_NOTE",
]

#: Результат честной исторической проверки САМОГО движка. Показывается
#: пользователю, чтобы «уверенность прогноза» не читалась как вероятность
#: успеха. Цифры — из backtest/results/directional_v4.md.
BACKTEST_NOTE = (
    "📈 <b>Честно о результатах.</b> Сигналы выдаются только по паттернам, "
    "проверенным на реальных данных двух независимых наборов "
    "(290 фьючерсных монет 4h за 2019-2026 и 63 спотовые 1h за 2021-2026). "
    "Основной сетап «разворот после паники»: win-rate 55-59%, profit factor "
    "1.44-1.48. Это НЕ гарантия: были и убыточные годы (2022 — PF 0.36). "
    "Уверенность откалибрована по факту: 60-65% → 51% прибыльных, "
    "65-70% → 56%, 70%+ → 59%."
)

DISCLAIMER = (
    "⚠️ <i>Не финансовый совет. Это статистическая оценка по рыночным данным; "
    "рынок может пойти против любого сигнала. Риск — только тот, который "
    "готовы потерять.</i>"
)


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    av = abs(value)
    if av >= 1000:
        return f"{value:,.2f}".replace(",", " ")
    if av >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if av >= 0.01:
        return f"{value:.5f}"
    return f"{value:.8f}".rstrip("0")


def _bar(pct: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _pct_label(pct: float) -> str:
    if pct >= 75:
        return "высокая"
    if pct >= 60:
        return "средняя"
    if pct >= 45:
        return "низкая"
    return "очень низкая"


# ---------------------------------------------------------------------------
# Карточка сигнала
# ---------------------------------------------------------------------------

def format_signal_card(sig: DirectionalSignal, *, compact: bool = False) -> str:
    """Основная карточка: направление, план, обе уверенности, объяснение."""
    lines: list[str] = []
    head = f"<b>{_esc(sig.base)}</b> · {_esc(sig.exchange)} · {sig.profile.title}"
    lines.append(head)
    price_line = f"💵 Цена: <b>{_fmt(sig.price)}</b>"
    if sig.timeframes_used:
        price_line += f"  ·  ТФ: {', '.join(sig.timeframes_used)}"
    lines.append(price_line)
    lines.append("")

    # --- направление и КРАТКАЯ ИНСТРУКЦИЯ -------------------------------------
    verdict = sig.direction_label
    if sig.direction != "none" and not sig.actionable:
        verdict += "  (наблюдение, вход не рекомендую)"
    lines.append(f"🎯 <b>Направление:</b> {verdict}")
    if sig.setup is not None:
        tag = "🧪 экспериментальный" if sig.setup.is_experimental else "✅ проверенный"
        lines.append(f"🧩 <b>Сетап:</b> {_esc(sig.setup.name)} <i>({tag})</i>")

    plan0 = sig.plan
    if plan0 is not None and sig.setup is not None:
        action = "КУПИТЬ (Long)" if sig.direction == "long" else "ПРОДАТЬ (Short)"
        arrow = "вырастет" if sig.direction == "long" else "упадёт"
        move2 = abs(plan0.targets[1] - plan0.entry_mid) / plan0.entry_mid * 100.0
        risk_move = plan0.risk_percent_of_price
        lines.append("")
        lines.append("📋 <b>Что делать — коротко</b>")
        lines.append(f"1️⃣ {action} в диапазоне <code>{_fmt(plan0.entry_low)} – {_fmt(plan0.entry_high)}</code>")
        lines.append(f"2️⃣ Сразу поставить стоп <code>{_fmt(plan0.stop)}</code> "
                     f"(если ошиблись — потеряете {plan0.risk_per_trade_percent:.1f}% депозита, не больше)")
        lines.append(f"3️⃣ Ожидание: цена {arrow} примерно на {move2:.1f}% "
                     f"(основная цель <code>{_fmt(plan0.targets[1])}</code>) за {_esc(sig.setup.horizon_text)}")
        lines.append(f"4️⃣ Размер позиции: <b>{plan0.position_percent_of_deposit:.0f}%</b> депозита "
                     f"без плеча — так стоп в {risk_move:.1f}% стоит ровно "
                     f"{plan0.risk_per_trade_percent:.1f}% счёта")
        if sig.calibration:
            lines.append(f"🎲 Шансы: {_esc(sig.calibration)}")

    # --- уверенности ---------------------------------------------------------
    lines.append("")
    lines.append("📊 <b>Две уверенности</b>")
    lines.append(
        f"• Данные:  {_bar(sig.data_confidence)} <b>{sig.data_confidence:.0f}%</b> "
        f"({_pct_label(sig.data_confidence)})"
    )
    lines.append(
        f"• Прогноз: {_bar(sig.signal_confidence)} <b>{sig.signal_confidence:.0f}%</b> "
        f"({_pct_label(sig.signal_confidence)})"
    )
    if sig.setup is not None:
        st = sig.setup
        lines.append(
            f"<i>уверенность — не «вероятность заработать», а оценка качества именно "
            f"этого сетапа (глубина обвала, волатильность, ликвидность, состояние рынка). "
            f"Она откалибрована по {st.stats.trades if st.stats else 0} реальным сделкам.</i>"
        )
    else:
        lines.append(
            f"<i>фоновая картина индикаторов: счёт {sig.context_score:+.2f}, "
            f"согласие {sig.agreement * 100:.0f}% — этого мало для сигнала</i>"
        )

    # --- план ----------------------------------------------------------------
    plan = sig.plan
    if plan is not None:
        lines.append("")
        lines.append("🧭 <b>План сделки</b>")
        lines.append(f"• Вход: <code>{_fmt(plan.entry_low)} – {_fmt(plan.entry_high)}</code>")
        lines.append(
            f"• Stop-Loss: <code>{_fmt(plan.stop)}</code> "
            f"(−{plan.risk_percent_of_price:.2f}% от входа)"
        )
        lines.append(
            f"• 🦾 Плечо: до <b>{plan.max_leverage:g}×</b> "
            f"<i>({_esc(plan.leverage_note)})</i>"
        )
        for i, (tgt, rr, share) in enumerate(
            zip(plan.targets, plan.target_rr, plan.target_shares), start=1
        ):
            move = abs(tgt - plan.entry_mid) / plan.entry_mid * 100.0
            lines.append(
                f"• TP{i}: <code>{_fmt(tgt)}</code> (+{move:.2f}%, R/R {rr:.2f}, "
                f"фикс ~{share}%)"
            )
        lines.append(f"• Горизонт: {_esc(plan.horizon)}")
        lines.append(
            f"• Размер: риск {plan.risk_per_trade_percent:.1f}% депозита ⇒ позиция "
            f"≈{plan.position_percent_of_deposit:.0f}% депозита без плеча "
            f"(с плечом {plan.max_leverage:g}× — "
            f"{plan.position_percent_of_deposit / plan.max_leverage:.0f}% маржи)"
        )
        lines.append(f"❌ <b>Отмена:</b> {_esc(plan.invalidation)}")

    # --- честная статистика сетапа --------------------------------------------
    if sig.setup is not None and sig.setup.stats is not None:
        st = sig.setup
        a, b = st.stats, st.stats_other
        lines.append("")
        lines.append("📊 <b>Как этот сетап работал на истории</b>")
        lines.append(f"• {_esc(a.sample)}: {a.trades} сделок, win-rate {a.win_rate:.0f}%, "
                     f"PF {a.profit_factor:.2f}")
        if b is not None:
            lines.append(f"• {_esc(b.sample)}: {b.trades} сделок, win-rate {b.win_rate:.0f}%, "
                         f"PF {b.profit_factor:.2f}")
        lines.append(f"• вне обучающей выборки (2024-07 → 2026): PF {a.test_pf:.2f}")
        lines.append(f"• худший год: {_esc(a.worst_year)} — убыточные периоды были и будут")
        if st.warnings:
            for w in st.warnings:
                lines.append(f"⚠️ {_esc(w)}")

    # --- контекст рынка -------------------------------------------------------
    ctx: list[str] = []
    if sig.btc_correlation is not None:
        strength = (
            "почти копирует BTC" if abs(sig.btc_correlation) > 0.85 else
            "сильно зависит от BTC" if abs(sig.btc_correlation) > 0.6 else
            "живёт своей жизнью"
        )
        ctx.append(f"корреляция с BTC {sig.btc_correlation:+.2f} — {strength}")
    if sig.long_short_ratio is not None:
        ctx.append(f"лонг/шорт {sig.long_short_ratio:.2f}")
    if sig.listing_age_days is not None:
        ctx.append(f"история ~{sig.listing_age_days:.0f} дн")
    if ctx:
        lines.append("")
        lines.append("🧩 <b>Контекст:</b> " + _esc("; ".join(ctx)))

    # --- почему НЕ вход -------------------------------------------------------
    if sig.why_not:
        lines.append("")
        lines.append("🚫 <b>Почему НЕ вход</b>")
        for reason in sig.why_not[:5]:
            lines.append(f"• {_esc(reason)}")

    # --- предупреждения -------------------------------------------------------
    if sig.quality.warnings and not compact:
        lines.append("")
        lines.append("⚠️ <b>Риски данных</b>")
        for w in sig.quality.warnings[:4]:
            lines.append(f"• {_esc(w)}")

    # --- объяснение -----------------------------------------------------------
    if not compact:
        lines.append("")
        lines.append("💬 <b>Простыми словами</b>")
        lines.append(_esc(sig.explanation))

    lines.append("")
    lines.append("👉 <i>Кнопка «📖 Разбор для новичка» — подробное объяснение сетапа.</i>")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_beginner_card(sig: DirectionalSignal) -> str:
    """Развёрнутое объяснение: 6 разделов «как трейдер учит новичка»."""
    lines = [f"📖 <b>{_esc(sig.base)} — разбор для новичка</b>", ""]
    for title, text in beginner_breakdown(sig, DEFAULT_CONFIG):
        lines.append(f"<b>{title}</b>")
        lines.append(_esc(text))
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_why_card(sig: DirectionalSignal) -> str:
    """Детальный разбор: все факторы с весами и вкладом + формула уверенности."""
    lines = [f"🔬 <b>{_esc(sig.base)} — из чего сложился вывод</b>", ""]
    lines.append("<b>Факторы</b> (score × вес = вклад):")
    rows = sorted(sig.factors, key=lambda f: abs(f.contribution), reverse=True)
    for f in rows:
        lines.append(
            f"{f.arrow()} <b>{_esc(f.title)}</b>: {f.score:+.2f} × {f.weight:.1f} = "
            f"{f.contribution:+.2f}"
        )
        lines.append(f"   <i>{_esc(f.note)}</i>")
    if sig.group_scores:
        lines.append("")
        lines.append("<b>Голоса независимых групп</b> (именно они формируют уверенность):")
        for cat, score in sorted(sig.group_scores.items(), key=lambda kv: -abs(kv[1])):
            icon = "🟢" if score > 0.15 else ("🔴" if score < -0.15 else "⚪️")
            lines.append(f"{icon} {_esc(cat)}: {score:+.2f}")
        lines.append(
            f"<i>согласных групп: {sig.agreeing_groups} — от этого зависит потолок "
            f"уверенности прогноза</i>"
        )
    lines.append("")
    lines.append("<b>Как считалась уверенность</b>")
    for row in sig.confidence_breakdown():
        lines.append(f"• {_esc(row)}")
    lines.append("")
    lines.append("<b>Качество данных по компонентам</b>")
    for name, value in sig.quality.components.items():
        lines.append(f"• {_esc(name)}: {value:.0f}/100")
    if sig.quality.warnings:
        lines.append("")
        lines.append("<b>Замечания к данным</b>")
        for w in sig.quality.warnings:
            lines.append(f"• {_esc(w)}")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_top_signals(
    signals: Sequence[DirectionalSignal],
    *,
    profile: RiskProfile,
    scanned: int,
    skipped: int = 0,
    elapsed: Optional[float] = None,
) -> str:
    """Список лучших сигналов рынка прямо сейчас."""
    lines = [
        f"🏆 <b>Топ сигналов сейчас</b> · {profile.title}",
        f"<i>просканировано монет: {scanned}"
        + (f", отсеяно по качеству данных: {skipped}" if skipped else "")
        + (f", за {elapsed:.1f}с" if elapsed else "")
        + "</i>",
        "",
    ]
    if not signals:
        lines.append("Сейчас ни одна монета не проходит ворота этого профиля.")
        lines.append(
            "Это нормальный результат: рынок бывает без внятных возможностей. "
            "Попробуйте профиль помягче или зайдите позже."
        )
        lines.append("")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    for i, sig in enumerate(signals, start=1):
        plan = sig.plan
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        head = (
            f"{medal} <b>{_esc(sig.base)}</b> {sig.direction_label} · "
            f"прогноз {sig.signal_confidence:.0f}% / данные {sig.data_confidence:.0f}%"
        )
        lines.append(head)
        if plan:
            lines.append(
                f"   вход <code>{_fmt(plan.entry_low)}–{_fmt(plan.entry_high)}</code> · "
                f"стоп <code>{_fmt(plan.stop)}</code> · TP1 <code>{_fmt(plan.targets[0])}</code> · "
                f"R/R {plan.rr:.2f} · до {plan.max_leverage:g}×"
            )
        top = sig.top_factors(2)
        if top:
            lines.append(f"   <i>{_esc('; '.join(f.note for f in top))}</i>")
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_accuracy_card(stats: dict[str, Any], recent: Sequence[Any], base: Optional[str]) -> str:
    """Фактическая точность прошлых сигналов — как есть, без прикрас."""
    title = f"📈 <b>Точность сигналов{f' по {_esc(base)}' if base else ''}</b>"
    lines = [title, ""]
    if not stats["total"]:
        lines.append(
            "Журнал пуст: бот ещё не выдавал сигналов "
            f"{'по этой монете' if base else ''} или журнал был сброшен при рестарте."
        )
        lines.append("")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    wr = stats["winrate"]
    lines.append(f"• Всего сигналов: <b>{stats['total']}</b> (открытых: {stats['open']})")
    lines.append(f"• Завершено: <b>{stats['closed']}</b>")
    if wr is None:
        lines.append("• Win-rate: пока нечего считать (нет закрытых сделок)")
    else:
        lines.append(
            f"• Win-rate: <b>{wr:.1f}%</b> ({stats['wins']} прибыльных / "
            f"{stats['losses']} убыточных)"
        )
    if stats["avg_result_percent"] is not None:
        lines.append(f"• Средний результат сделки: <b>{stats['avg_result_percent']:+.2f}%</b>")
        lines.append(f"• Сумма результатов: <b>{stats['sum_result_percent']:+.2f}%</b>")
    if stats.get("avg_rr") is not None:
        lines.append(f"• Средний плановый R/R: <b>{stats['avg_rr']:.2f}</b>")
    if stats["profit_factor"] is not None:
        lines.append(f"• Profit factor: <b>{stats['profit_factor']:.2f}</b>")
    by = stats["by_status"]
    lines.append(
        f"• Разбивка: TP1 {by.get('tp1', 0)} · TP2 {by.get('tp2', 0)} · "
        f"TP3 {by.get('tp3', 0)} · стоп {by.get('stop', 0)} · истёк {by.get('expired', 0)}"
    )
    if stats["closed"] and stats["closed"] < 20:
        lines.append("")
        lines.append(
            f"<i>⚠️ Выборка {stats['closed']} сделок статистически ничтожна — "
            f"по ней НЕЛЬЗЯ судить о качестве бота. Нужны десятки-сотни.</i>"
        )
    if recent:
        lines.append("")
        lines.append("<b>Последние</b>")
        for rec in recent[:8]:
            when = time.strftime("%d.%m %H:%M", time.gmtime(rec.created_at))
            res = f"{rec.result_percent:+.2f}%" if rec.result_percent is not None else "—"
            icon = {"tp1": "✅", "tp2": "✅", "tp3": "✅", "stop": "❌",
                    "expired": "⏳", "open": "🔄"}.get(rec.status, "•")
            lines.append(
                f"{icon} {when} {_esc(rec.base)} {rec.direction.upper()} "
                f"conf {rec.signal_confidence:.0f}% → {_esc(rec.status)} {res}"
            )
    lines.append("")
    lines.append(BACKTEST_NOTE)
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_watchlist_card(items: Sequence[str], alerts: Sequence[str]) -> str:
    lines = ["👁 <b>Watchlist</b>", ""]
    if not items:
        lines.append("Список пуст. Добавьте монету кнопкой «➕ В watchlist» из её карточки.")
    else:
        lines.append("Отслеживаю: " + ", ".join(f"<code>{_esc(i)}</code>" for i in items))
        lines.append("")
        lines.append(
            "<i>Как только по монете появится сигнал, проходящий ворота вашего "
            "риск-профиля, пришлю карточку сюда.</i>"
        )
    if alerts:
        lines.append("")
        lines.append("Последние алерты: " + ", ".join(_esc(a) for a in alerts[-5:]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def directional_menu_keyboard(profile_key: str = "moderate") -> dict[str, Any]:
    """Главное меню направленного анализа — всё доступно кнопками."""
    def mark(key: str, text: str) -> str:
        return ("• " + text + " •") if key == profile_key else text
    return {
        "inline_keyboard": [
            [
                {"text": "🏆 Топ сигналов сейчас", "callback_data": "scan"},
            ],
            [
                {"text": "🔎 Выбрать монету", "callback_data": "find"},
                {"text": "🧩 Что ищет бот", "callback_data": "setups"},
            ],
            [
                {"text": "💠 BTC", "callback_data": "an:BTC"},
                {"text": "💠 ETH", "callback_data": "an:ETH"},
                {"text": "💠 SOL", "callback_data": "an:SOL"},
            ],
            [
                {"text": mark("conservative", "🛡 Осторожно"), "callback_data": "profile:conservative"},
                {"text": mark("moderate", "⚖️ Средне"), "callback_data": "profile:moderate"},
                {"text": mark("aggressive", "🔥 Агрессивно"), "callback_data": "profile:aggressive"},
            ],
            [
                {"text": "👁 Watchlist", "callback_data": "watch"},
                {"text": "📈 Точность", "callback_data": "accuracy"},
            ],
            [
                {"text": "🔀 Арбитраж-меню", "callback_data": "help"},
            ],
        ]
    }


def signal_keyboard(base: str, *, in_watchlist: bool = False) -> dict[str, Any]:
    """Кнопки под карточкой сигнала конкретной монеты."""
    base = base.upper()
    watch_text = "🗑 Из watchlist" if in_watchlist else "➕ В watchlist"
    watch_cmd = f"unwatch:{base}" if in_watchlist else f"watch:{base}"
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Обновить", "callback_data": f"an:{base}"},
                {"text": watch_text, "callback_data": watch_cmd},
            ],
            [
                {"text": "🔍 Показать почему", "callback_data": f"why:{base}"},
                {"text": "📖 Для новичка", "callback_data": f"learn:{base}"},
            ],
            [
                {"text": "🧩 Что ищет бот", "callback_data": "setups"},
                {"text": "📈 Точность", "callback_data": f"accuracy:{base}"},
            ],
            [
                {"text": "🛡", "callback_data": f"profile:conservative:{base}"},
                {"text": "⚖️", "callback_data": f"profile:moderate:{base}"},
                {"text": "🔥", "callback_data": f"profile:aggressive:{base}"},
                {"text": "🔎 Другая монета", "callback_data": "find"},
            ],
            [
                {"text": "🧭 Меню", "callback_data": "menu"},
            ],
        ]
    }


def profiles_keyboard() -> dict[str, Any]:
    rows = [
        [{"text": p.title, "callback_data": f"profile:{key}"}]
        for key, p in RISK_PROFILES.items()
    ]
    rows.append([{"text": "🏆 Топ сигналов", "callback_data": "scan"}])
    return {"inline_keyboard": rows}
