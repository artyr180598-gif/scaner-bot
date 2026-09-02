"""
app/presentation/render.py — рендеринг интерфейса (русский язык, HTML).

Форматирование вынесено из Telegram-слоя: эти функции возвращают обычный
текст, поэтому их можно тестировать без сети и переиспользовать в CLI/вебе.

Дизайн-принципы:
  * сначала ответ (направление + уверенность), потом цифры, потом объяснение;
  * каждое число подписано — новичок не должен угадывать, что значит «0.1234»;
  * честность: рядом с уверенностью всегда есть качество данных и риск-блок,
    а в подвале — напоминание, что это не инвестиционная рекомендация.
"""

from __future__ import annotations

from html import escape as _escape
from typing import Iterable, List, Optional, Sequence

from app.analysis.base import Group
from app.domain.models import (Direction, MarketContext, ScanReport, Signal,
                               Timeframe)
from app.presentation.format import (ago, base_of, clock, fmt_confidence,
                                     fmt_number, fmt_pct, fmt_price, fmt_ratio,
                                     fmt_usd, gauge, plural, progress_bar)
from app.scoring.scorer import ScoreResult, confidence_label, potential_label

DISCLAIMER = "⚠️ Не является индивидуальной инвестиционной рекомендацией."

GROUP_TITLES = {
    Group.TREND: "Тренд",
    Group.MOMENTUM: "Импульс",
    Group.STRUCTURE: "Структура",
    Group.SMC: "Смарт-мани",
    Group.VOLUME: "Объём",
    Group.LEVELS: "Уровни",
    Group.DERIVATIVES: "Деривативы",
    Group.CONTEXT: "Контекст рынка",
    Group.SENTIMENT: "Новости",
    Group.POTENTIAL: "Потенциал",
    Group.QUALITY: "Качество данных",
}

DIRECTION_TITLES = {
    Direction.LONG: "🚀 LONG сигнал",
    Direction.SHORT: "🔻 SHORT сигнал",
    Direction.WAIT: "⏸ ЖДЁМ — сигнала нет",
}

TF_TITLES = {
    Timeframe.M5: "5m", Timeframe.M15: "15m", Timeframe.M30: "30m",
    Timeframe.H1: "1H", Timeframe.H4: "4H", Timeframe.D1: "1D",
}

LINE = "━━━━━━━━━━━━━━━━━━━━"


def esc(text: str) -> str:
    return _escape(str(text), quote=False)


# ---------------------------------------------------------------------------
# Карточка сигнала
# ---------------------------------------------------------------------------

def render_signal(
    signal: Signal,
    *,
    deposit: float = 1000.0,
    show_disclaimer: bool = True,
    context: Optional[MarketContext] = None,
) -> str:
    """Главная карточка сигнала — то, что пользователь видит в первую очередь."""
    if signal.direction is Direction.WAIT or signal.plan is None:
        return render_wait(signal, context=context)

    plan = signal.plan
    base = base_of(signal.symbol)
    header = (
        f"{DIRECTION_TITLES[signal.direction]} · <b>${esc(base)}</b>\n"
        f"Уверенность: <b>{fmt_confidence(signal.confidence)}</b> "
        f"({confidence_label(signal.confidence)})\n"
        f"<i>Сетап:</i> {esc(signal.setup)}"
    )

    sizing = plan.position_size(deposit)
    entry = f"{fmt_price(plan.entry_low)} – {fmt_price(plan.entry_high)}"
    stop = (f"{fmt_price(plan.stop)} ({fmt_pct(plan.stop_pct)})")

    lines: List[str] = [
        header, LINE,
        f"💰 <b>Вход:</b> {entry}",
        f"🛑 <b>Стоп:</b> {stop}",
        "🎯 <b>Цели:</b>",
    ]
    icons = ("1️⃣", "2️⃣", "3️⃣", "4️⃣")
    for i, target in enumerate(plan.targets):
        icon = icons[i] if i < len(icons) else "•"
        fraction = f" · фикс {int(target.fraction * 100)}%" if target.fraction else ""
        lines.append(
            f"  {icon} <b>TP{i + 1}</b>: {fmt_price(target.price)} "
            f"({fmt_pct(target.pct)}) · R {target.rr:.1f}{fraction}"
        )

    lines += [
        LINE,
        f"⚖️ <b>R:R</b> — {fmt_ratio(plan.rr_avg)} (до TP1 {fmt_ratio(plan.rr_primary)})",
        f"📦 <b>Объём:</b> {fmt_usd(sizing['notional'], 0)} при риске "
        f"{fmt_usd(sizing['risk_usd'], 0)} на сделку · плечо ≤ {sizing['leverage']:.1f}x",
        f"⏱ <b>Горизонт:</b> ~{signal.horizon_hours} ч · "
        f"ТФ: {TF_TITLES.get(signal.timeframe, signal.timeframe.value)} · "
        f"данные {int(signal.data_confidence * 100)}%",
        f"📍 Цена сейчас: {fmt_price(signal.price)} · {esc(signal.exchange)}",
        LINE,
        "💡 <b>Почему именно эта монета и почему сейчас</b>",
        esc(signal.summary),
    ]
    if signal.tags:
        lines.append("🏷 " + " · ".join(esc(t) for t in signal.tags))
    if context is not None:
        lines.append(f"🌐 {esc(context.describe_ru())}")
    if show_disclaimer:
        lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_wait(signal: Signal, *, context: Optional[MarketContext] = None) -> str:
    """Карточка «сигнала нет» — с объяснением, а не пустым ответом."""
    base = base_of(signal.symbol)
    lines = [
        f"⏸ <b>${esc(base)}</b> — сигнала нет (ЖДЁМ)",
        f"Оценка направления: {signal.score:+.2f} · уверенность "
        f"{fmt_confidence(signal.confidence)}",
        LINE,
        esc(signal.summary),
    ]
    if signal.metrics:
        m = signal.metrics
        bits: List[str] = []
        if "change_7d" in m and m["change_7d"] == m["change_7d"]:
            bits.append(f"7д {m['change_7d']:+.1f}%")
        if "compression" in m and m["compression"] == m["compression"]:
            bits.append(f"сжатие {m['compression'] * 100:.0f}%")
        if "change_z" in m and m["change_z"] == m["change_z"]:
            bits.append(f"z-изменение {m['change_z']:+.1f}")
        if bits:
            lines.append("📊 " + " · ".join(bits))
    if context is not None:
        lines.append(f"🌐 {esc(context.describe_ru())}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Списки
# ---------------------------------------------------------------------------

def render_top_signals(report: ScanReport, limit: int = 8,
                       deposit: float = 1000.0) -> str:
    """🔥 Топ сигналов сейчас: компактный список с главным."""
    signals = report.top(limit)
    header = (
        "🔥 <b>Топ сигналы сейчас</b>\n"
        f"Проверено {report.scanned} "
        f"{plural(report.scanned, 'монета', 'монеты', 'монет')} из {report.universe_size} · "
        f"обновлено {ago(report.finished_at)}"
    )
    if not signals:
        return (
            f"{header}\n{LINE}\n"
            "Сейчас нет setups, которые прошли бы все гейты качества.\n"
            "Это нормально: бот молчит, когда нечего сказать — лучше пропустить "
            "сделку, чем войти в лотерею.\n\n"
            f"🌐 {esc(report.context.describe_ru())}"
        )
    lines = [header, LINE]
    for i, s in enumerate(signals, start=1):
        plan = s.plan
        arrow = "🚀" if s.direction is Direction.LONG else "🔻"
        rr = fmt_ratio(plan.rr_primary) if plan else "—"
        stop = fmt_pct(plan.stop_pct) if plan else "—"
        tp1 = fmt_price(plan.targets[0].price) if plan and plan.targets else "—"
        lines.append(
            f"{i}. {arrow} <b>${esc(base_of(s.symbol))}</b> · "
            f"{fmt_confidence(s.confidence)} · R:R {rr}\n"
            f"   вход {fmt_price(plan.entry_low)}–{fmt_price(plan.entry_high)} · "
            f"стоп {fmt_price(plan.stop)} ({stop}) · TP1 {tp1}\n"
            f"   <i>{esc(s.setup)}</i>"
        )
    lines += [LINE, "Нажмите на монету, чтобы открыть полный план сделки."]
    lines.append(f"🌐 {esc(report.context.describe_ru())}")
    return "\n".join(lines)


def render_scanner(report: ScanReport, limit: int = 15) -> str:
    """
    📈 Сканер рынка: таблица пре-скрининга — кто «на взводе», даже если
    полного сигнала ещё нет.
    """
    candidates = report.candidates[:limit]
    header = (
        "📈 <b>Сканер рынка</b> — монеты с накопленной энергией\n"
        f"Вселенная: {report.universe_size} · прошло отбор: {len(report.candidates)}"
    )
    if not candidates:
        return f"{header}\n{LINE}\nКандидатов нет: рынок либо мёртв, либо уже бежит."
    lines = [header, LINE]
    for i, c in enumerate(candidates, start=1):
        m = c.metrics
        comp = m.get("compression", 0.0)
        change7 = m.get("change_7d")
        vol = m.get("quote_volume", c.ticker.quote_volume if c.ticker else 0.0)
        bar = progress_bar(comp, 8)
        change_txt = f"{change7:+.1f}%/7д" if change7 is not None else \
            f"{c.ticker.change_pct:+.1f}%/24ч"
        lines.append(
            f"{i:2}. <b>${esc(base_of(c.symbol))}</b> {bar} {comp * 100:3.0f}% · "
            f"{change_txt} · {fmt_usd(vol)}"
        )
        if c.reasons:
            lines.append(f"     <i>{esc(c.reasons[0])}</i>")
    lines += [
        LINE,
        "Шкала — «сжатость пружины»: чем выше, тем сильнее сжата волатильность "
        "и тем вероятнее импульс (но направление скажет только глубокий анализ).",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Глубокий анализ
# ---------------------------------------------------------------------------

def render_deep_analysis(signal: Signal, *, deposit: float = 1000.0) -> str:
    """Полный разбор по кнопке «Детали»: все группы факторов с оценками."""
    base = base_of(signal.symbol)
    lines = [
        f"🔬 <b>Глубокий анализ ${esc(base)}</b>",
        f"Цена: {fmt_price(signal.price)} · ТФ "
        f"{TF_TITLES.get(signal.timeframe, signal.timeframe.value)} · "
        f"{esc(signal.exchange)}",
        f"Итог: <b>{signal.direction.value}</b> · уверенность "
        f"{fmt_confidence(signal.confidence)} · потенциал движения "
        f"{int(_potential_of(signal) * 100)}%",
        LINE,
        "<b>Оценки по независимым группам</b>",
    ]
    groups = _group_view(signal)
    for title, score, n_factors, is_directional in groups:
        if is_directional:
            bar = _signed_bar(score)
            value = f"{score:+.2f}"
        else:
            bar = gauge(abs(score), 10)
            value = f"{abs(score):.2f}"
        lines.append(f"  {title:<12} {bar} {value}  <i>({n_factors})</i>")

    lines += [LINE, "<b>Главные аргументы</b>"]
    for text in _all_evidence(signal)[:8]:
        lines.append(f"• {esc(text)}")

    metrics = signal.metrics
    if metrics:
        lines += [LINE, "<b>Метрики</b>"]
        rows: List[str] = []
        for key, label in (
            ("change_24h", "24ч"), ("change_7d", "7д"), ("change_30d", "30д"),
        ):
            if metrics.get(key) == metrics.get(key) and key in metrics:
                rows.append(f"{label} {metrics[key]:+.1f}%")
        if "atr_percentile" in metrics:
            rows.append(f"ATR-перцентиль {metrics['atr_percentile']:.0f}")
        if "volume_z" in metrics and metrics["volume_z"] == metrics["volume_z"]:
            rows.append(f"объём z={metrics['volume_z']:.1f}")
        if "funding_pct" in metrics:
            rows.append(f"funding {metrics['funding_pct']:+.3f}%")
        if "oi_change_pct" in metrics:
            rows.append(f"OI {metrics['oi_change_pct']:+.1f}%")
        if "quote_volume_usd" in metrics:
            rows.append(f"оборот {fmt_usd(metrics['quote_volume_usd'])}")
        lines.append("  " + " · ".join(rows))

    lines += [LINE, esc(signal.summary), DISCLAIMER]
    return "\n".join(lines)


def _potential_of(signal: Signal) -> float:
    group = signal.factors.by_group().get(Group.POTENTIAL)
    if not group:
        return 0.5
    weights = sum(f.weight for f in group) or len(group)
    return max(0.0, min(1.0, sum(f.score * f.weight for f in group) / weights))


def _group_view(signal: Signal) -> List[tuple]:
    """(название, оценка, число факторов, направленная ли группа)."""
    out: List[tuple] = []
    by_group = signal.factors.by_group()
    order = [Group.TREND, Group.MOMENTUM, Group.STRUCTURE, Group.SMC, Group.VOLUME,
             Group.LEVELS, Group.DERIVATIVES, Group.CONTEXT, Group.SENTIMENT,
             Group.POTENTIAL, Group.QUALITY]
    for group in order:
        factors = by_group.get(group)
        if not factors:
            continue
        weights = sum(f.weight for f in factors) or len(factors)
        if group in (Group.POTENTIAL, Group.QUALITY):
            score = sum(abs(f.score) * f.weight for f in factors) / weights
            is_directional = False
        else:
            score = sum(f.score * f.weight for f in factors) / weights
            is_directional = True
        out.append((GROUP_TITLES.get(group, group), score, len(factors), is_directional))
    return out


def _signed_bar(score: float, width: int = 10) -> str:
    """Двусторонняя шкала: медвежьи баллы слева, бычьи справа."""
    half = width // 2
    filled = int(round(abs(score) * half))
    filled = max(0, min(half, filled))
    if score >= 0:
        return "░" * half + "█" * filled + "·" * (half - filled)
    return "·" * (half - filled) + "█" * filled + "░" * half


def _all_evidence(signal: Signal, limit: int = 12) -> List[str]:
    factors = sorted(signal.factors.factors,
                     key=lambda f: -abs(f.score) * f.weight)
    out: List[str] = []
    for f in factors:
        if not f.evidence or abs(f.score) < 0.1:
            continue
        prefix = "▲" if f.score > 0 else ("▼" if f.score < 0 else "•")
        out.append(f"{prefix} {f.evidence}")
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Меню, помощь, настройки, статистика
# ---------------------------------------------------------------------------

def render_menu(last_scan: Optional[ScanReport] = None) -> str:
    lines = [
        "🧠 <b>Crypto Advisor</b> — советник по крипторынку",
        "",
        "Ищу монеты, у которых есть <b>задатки сильного движения</b> "
        "(сжатие волатильности, накопление, ликвидность), а не те, что уже улетели. "
        "По каждой — мульти-факторный анализ, план сделки и честная уверенность.",
        "",
        "<b>Что выбрать:</b>",
        "🔍 <b>Найти перспективные монеты</b> — полный скан рынка (2–4 мин)",
        "📊 <b>Глубокий анализ монеты</b> — разбор конкретной монеты",
        "🔥 <b>Топ сигналы сейчас</b> — лучшие идеи последнего скана",
        "📈 <b>Сканер рынка</b> — кто «на взводе» прямо сейчас",
        "⭐ <b>Мой список</b> — наблюдение за своими монетами",
        "⚙️ <b>Настройки</b> — риск, депозит, пороги, авто-рассылка",
        "ℹ️ <b>Помощь</b> — как читать сигналы",
    ]
    if last_scan is not None:
        lines += [
            "",
            f"🕒 Последний скан: {ago(last_scan.finished_at)} · "
            f"проверено {last_scan.scanned} монет · "
            f"{len(last_scan.actionable)} "
            f"{plural(len(last_scan.actionable), 'сигнал', 'сигнала', 'сигналов')}",
        ]
    return "\n".join(lines)


def render_help() -> str:
    return "\n".join([
        "ℹ️ <b>Помощь</b>",
        LINE,
        "<b>Как работает бот</b>",
        "1. Берёт всю ликвидную вселенную биржи (сотни пар).",
        "2. Отсеивает то, что уже улетело, и неликвид.",
        "3. Ищет «сжатые пружины»: низкая волатильность при растущих объёмах, "
        "открытых позициях и близости к уровню.",
        "4. По каждой монете считает 30+ факторов в 10 независимых группах: "
        "тренд, импульс, структура (BOS/CHOCH), смарт-мани (Order Blocks, FVG), "
        "объём, уровни, деривативы (funding/OI), контекст BTC, новости.",
        "5. Строит план: зона входа, стоп за структурой, 3 цели, R:R, объём позиции.",
        "6. Публикует только то, что прошло все гейты качества.",
        LINE,
        "<b>Как читать сигнал</b>",
        "• <b>Уверенность</b> — сила согласия независимых групп аргументов, "
        "а НЕ вероятность прибыли. 8/10 не значит «80% шанс».",
        "• <b>Потенциал</b> — вероятность сильного движения вообще "
        "(даже если направление угадано неверно).",
        "• <b>Вход зоной</b> — ставьте лимитный ордер внутри диапазона, "
        "не покупайте по рынку выше зоны.",
        "• <b>Стоп</b> — точка, где идея сломана. Без стопа сигнал не имеет смысла.",
        "• <b>TP1/TP2/TP3</b> — фиксируйте часть позиции на каждой цели и "
        "переносите стоп в безубыток после TP1.",
        "• <b>R:R</b> — сколько прибыли на единицу риска. Меньше 1.5 — не берём.",
        LINE,
        "<b>Команды</b>",
        "/start — главное меню",
        "/scan — запустить сканирование",
        "/top — топ сигналов последнего скана",
        "/market — сканер рынка (список «на взводе»)",
        "/coin BTC — анализ конкретной монеты",
        "/watch — список наблюдения",
        "/stats — точность прошлых сигналов",
        "/health — состояние бирж и кэша",
        LINE,
        "<b>Ограничения (честно)</b>",
        "• Бот не торгует и не хранит ваши деньги — только анализ.",
        "• Никакая «уверенность» не отменяет риск: крипторынок может пойти против "
        "любого сценария за одну новость.",
        "• Риск на сделку — 1–2% депозита максимум.",
        DISCLAIMER,
    ])


def render_settings(settings) -> str:
    return "\n".join([
        "⚙️ <b>Настройки</b>",
        LINE,
        f"🎯 Риск-профиль: <b>{esc(settings.risk_profile)}</b>",
        f"💵 Депозит: <b>{fmt_usd(settings.deposit_usd)}</b>",
        f"🧮 Риск на сделку: по профилю (см. пресет)",
        f"🔔 Уверенность для сигнала: <b>{settings.min_confidence:.1f}/10</b>",
        f"⚖️ Минимальный R:R: <b>{settings.min_rr:.1f}</b>",
        f"⏱ Таймфрейм сигналов: <b>{esc(settings.signal_timeframe)}</b>",
        f"📣 Авто-рассылка сигналов: <b>{'вкл' if settings.auto_push else 'выкл'}</b>",
        f"🎓 Подсказки для новичка: <b>{'вкл' if settings.show_beginner_hints else 'выкл'}</b>",
        LINE,
        "Измените параметр кнопками ниже. Настройки влияют на то, какие сигналы "
        "бот показывает и какой объём позиции предлагает.",
    ])


def render_stats(journal_stats: dict, scanner_stats: dict,
                 health: Optional[Sequence[str]] = None) -> str:
    lines = ["📊 <b>Статистика и качество</b>", LINE]
    if scanner_stats:
        lines += [
            "<b>Последний скан</b>",
            f"  вселенная: {scanner_stats.get('universe', 0)} · "
            f"проверено: {scanner_stats.get('scanned', 0)}",
            f"  сигналов: {scanner_stats.get('actionable', 0)} "
            f"(лонгов {scanner_stats.get('longs', 0)}, "
            f"шортов {scanner_stats.get('shorts', 0)})",
            f"  средняя уверенность: {scanner_stats.get('avg_confidence', 0):.1f}/10 · "
            f"длительность {scanner_stats.get('duration_s', 0):.0f} с",
        ]
    lines.append(LINE)
    lines.append("<b>Исходы прошлых сигналов</b>")
    if journal_stats.get("closed"):
        lines += [
            f"  закрыто: {journal_stats['closed']} · "
            f"в работе: {journal_stats.get('open', 0)}",
            f"  целей достигнут: {journal_stats['win_rate']:.0f}% · "
            f"стопов: {journal_stats['stop_rate']:.0f}%",
            f"  средний результат: {journal_stats['avg_r']:+.2f}R "
            f"(суммарно {journal_stats['total_r']:+.1f}R)",
        ]
        buckets = journal_stats.get("calibration") or []
        if buckets:
            lines.append("  <i>Точность по уверенности:</i>")
            for b in buckets:
                lines.append(
                    f"    {b['range']}/10 — {b['n']} шт., "
                    f"win {b['win_rate']:.0f}%, {b['avg_r']:+.2f}R")
            lines.append("  Если winrate не растёт с уверенностью — доверяйте "
                         "статистике, а не цифре в сигнале.")
    else:
        lines.append("  " + str(journal_stats.get("note", "пока нет данных")))
    if health:
        lines += [LINE, "<b>Состояние источников</b>"]
        lines += [f"  {esc(h)}" for h in health]
    return "\n".join(lines)


def render_watchlist(symbols: Sequence[str]) -> str:
    if not symbols:
        return ("⭐ <b>Мой список</b>\n" + LINE +
                "Список пуст. Добавьте монету кнопкой «В список» в карточке "
                "анализа или командой /watch BTC.")
    lines = [f"⭐ <b>Мой список</b> — {len(symbols)} "
             f"{plural(len(symbols), 'монета', 'монеты', 'монет')}", LINE]
    for s in symbols:
        lines.append(f"  • <b>${esc(base_of(s))}</b>")
    lines.append("Нажмите на монету, чтобы получить свежий анализ.")
    return "\n".join(lines)


def render_progress(message: str, ratio: float) -> str:
    return f"⏳ {esc(message)}\n{progress_bar(ratio, 20)} {int(ratio * 100)}%"


def render_analysis_choice(report: Optional[ScanReport]) -> str:
    """Клавиатура «выберите монету для анализа»."""
    lines = ["📊 <b>Глубокий анализ монеты</b>",
             "Введите тикер (например, <code>BTC</code>) или выберите из списка:"]
    if report and report.candidates:
        lines.append(LINE)
        for c in report.candidates[:12]:
            comp = c.metrics.get("compression", 0.0)
            lines.append(f"  • ${esc(base_of(c.symbol))} — сжатие {comp * 100:.0f}%")
    return "\n".join(lines)
