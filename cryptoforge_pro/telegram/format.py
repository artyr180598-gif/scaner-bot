"""HTML-formatted messages for Telegram (aiogram HTML parse mode)."""

from __future__ import annotations

import html
from typing import Any

from cryptoforge_pro.config import Settings
from cryptoforge_pro.models import MarketData, Signal

LINE = "━━━━━━━━━━━━━━━━━━━━"


def format_price(value: float, base: str | None = None) -> str:
    value = float(value)
    if value == 0:
        return "N/A"
    if abs(value) >= 1000:
        text = f"{value:,.2f}"
    elif abs(value) >= 1:
        text = f"{value:,.4f}"
    elif abs(value) >= 0.01:
        text = f"{value:.6f}"
    else:
        text = f"{value:.10f}".rstrip("0").rstrip(".")
    return f"${text}"


def format_signal(signal: Signal, *, show_header: bool = True) -> str:
    direction_emoji = "🟢" if signal.direction == "LONG" else "🔴"
    lines: list[str] = []
    if show_header:
        lines.append(f"🔥 <b>#Сетап</b> | {signal.symbol} | {direction_emoji} <b>{signal.direction}</b>")
    lines.append(LINE)
    lines.append(
        f"🔹 <b>${signal.base.upper()}</b> | <b>{signal.direction}</b>"
    )
    lines.append(f"🎯 Вход: <code>{format_price(signal.entry_low)} – {format_price(signal.entry_high)}</code>")
    lines.append(f"🛑 Стоп: <code>{format_price(signal.stop_loss)}</code>")
    lines.append(f"🎁 TP1: <code>{format_price(signal.tp1)}</code> (R:R 1:{signal.rr1:.1f})")
    lines.append(f"🎁 TP2: <code>{format_price(signal.tp2)}</code> (R:R 1:{signal.rr2:.1f})")
    lines.append(f"🎁 TP3: <code>{format_price(signal.tp3)}</code> (R:R 1:{signal.rr3:.1f})")
    lines.append(f"📊 Уверенность: <b>{int(round(signal.confidence)):.0f}%</b>")
    lines.append(f"⏱ Таймфрейм: {signal.timeframe} · {signal.direction.lower()}")
    lines.append(LINE)
    lines.append("📝 <b>Обоснование:</b>")
    lines.append(html.escape(signal.rationale))
    lines.append("")
    lines.append("⚠️ <b>Риски:</b>")
    lines.append(html.escape(signal.risks))
    lines.append("")
    lines.append("<i>*Не является финансовой рекомендацией</i>")
    return "\n".join(lines)


def format_signal_short(signal: Signal) -> str:
    d = "🟢 L" if signal.direction == "LONG" else "🔴 S"
    return (
        f"<b>${signal.base.upper()}</b> {d} · {signal.timeframe} · "
        f"уверенность {signal.confidence:.0f}% · R:R {signal.rr2:.1f}"
    )


def format_deep_analysis(data: MarketData, settings: Settings) -> str:
    safe_symbol = html.escape(data.symbol)
    price = format_price(data.price, data.base)
    ticker = data.ticker
    der = data.derivatives
    candles = data.candles
    primary_tf = "1h" if "1h" in candles else (next(iter(candles), "1h"))
    primary_candles = candles.get(primary_tf, [])
    closes = [c.close for c in primary_candles]
    last = primary_candles[-1] if primary_candles else None
    change_h = 0.0
    if last is not None:
        prev = primary_candles[-2] if len(primary_candles) > 1 else last
        if prev.close:
            change_h = (last.close - prev.close) / prev.close * 100.0
    atr_pct = 0.0
    if last and last.close:
        from cryptoforge_pro.analysis import indicators as ta

        atr_v = ta.atr(primary_candles, 14)
        atr_pct = atr_v / last.close * 100.0 if atr_v else 0.0
    rsi = 0.0
    ema20 = ema50 = 0.0
    if closes:
        from cryptoforge_pro.analysis import indicators as ta

        rsi = ta.rsi(closes, 14) or 0.0
        ema20 = ta.ema(closes, 20) or 0.0
        ema50 = ta.ema(closes, 50) or 0.0
    lines: list[str] = []
    lines.append(f"🔍 <b>Глубокий анализ</b> | <code>{safe_symbol}</code>")
    lines.append(LINE)
    lines.append(f"💵 Текущая цена: <code>{price}</code>")
    lines.append(f"⏱ Таймфрейм: {primary_tf}")
    if ticker:
        lines.append(f"📊 24h: {ticker.change_24h_pct:+.2f}% · high {format_price(ticker.high_24h)} · low {format_price(ticker.low_24h)}")
        lines.append(f"💧 Объём 24h: ${ticker.volume_24h_quote:,.0f}")
    lines.append(f"📐 ATR: {atr_pct:.2f}% | RSI({primary_tf}): {rsi:.1f}")
    if ema20 and ema50:
        lines.append(f"📈 EMA20: <code>{format_price(ema20)}</code> · EMA50: <code>{format_price(ema50)}</code>")
    lines.append(f"🔄 За последний бар: {change_h:+.2f}%")
    if der:
        if der.funding_rate is not None:
            lines.append(f"💰 Funding: {der.funding_rate * 100:+.4f}%/8h")
        if der.open_interest_value_usd is not None:
            lines.append(f"💰 Open Interest: ${der.open_interest_value_usd:,.0f}")
        if der.open_interest is not None and der.open_interest_value_usd is None:
            lines.append(f"💰 Open Interest: {der.open_interest:,.0f}")
        if der.liquidation_24h_usd is not None:
            lines.append(f"💥 Ликвидации 24h: ${der.liquidation_24h_usd:,.0f}")
    if candles:
        lines.append(f"🗂 Свечи: {', '.join(f'{k}:{len(v)}' for k, v in candles.items())}")
    if data.news:
        top = data.news[0]
        lines.append(f"📰 Новость: {html.escape(top.get('title', '') or '')}")
    if data.btc_change_24h_pct:
        lines.append(
            f"🔗 BTC 24h: {data.btc_change_24h_pct:+.2f}% · корреляция: {data.btc_corr_returns:+.2f}"
        )
    lines.append(LINE)
    lines.append("<i>*Данные из Binance/Bybit (реальные), Coinglass только при API-ключе.</i>")
    return "\n".join(lines)


def format_error(message: str) -> str:
    return f"⚠️ <b>Не удалось получить рынок</b>\n\n{html.escape(message)}\n\nБот не использует заглушки: если биржа недоступна, он честно сообщает об этом."


def format_market_overview(data: dict) -> str:
    lines: list[str] = ["📊 <b>Обзор рынка</b>", LINE]
    btc: Any | None = data.get("btc")
    eth: Any | None = data.get("eth")

    def coin_line(label: str, ticker: Any | None) -> str:
        if ticker is None:
            return f"• <b>{label}</b>: недоступно"
        return (
            f"• <b>{label}</b>: <code>{format_price(ticker.last_price)}</code> "
            f"({ticker.change_24h_pct:+.2f}%) · объём ${ticker.volume_24h_quote:,.0f}"
        )

    lines.append(coin_line("BTC", btc))
    lines.append(coin_line("ETH", eth))
    lines.append(LINE)
    lines.append(f"📈 Восходящих: <b>{data.get('up', 0)}</b> · 📉 Нисходящих: <b>{data.get('down', 0)}</b>")
    lines.append(f"⚖️ Среднее изменение 24h: <b>{data.get('avg_change', 0):+.2f}%</b>")
    lines.append(f"💧 Суммарный объём: <b>${data.get('total_volume', 0):,.0f}</b>")
    fng: Any | None = data.get("fng")
    if fng:
        lines.append(f"😱/😄 Fear & Greed: <b>{fng.get('value')}</b> ({html.escape(str(fng.get('classification', '')))})")
    lines.append(LINE)
    lines.append("<b>🟢 Топ роста:</b>")
    for t in data.get("gainers", []):
        lines.append(f"• {html.escape(t.base)}: <code>{format_price(t.last_price)}</code> ({t.change_24h_pct:+.2f}%)")
    lines.append("<b>🔴 Топ падения:</b>")
    for t in data.get("losers", []):
        lines.append(f"• {html.escape(t.base)}: <code>{format_price(t.last_price)}</code> ({t.change_24h_pct:+.2f}%)")
    lines.append(LINE)
    lines.append("<i>Данные бирж в реальном времени; F&G может быть недоступен.</i>")
    return "\n".join(lines)


def format_news(items: list[dict]) -> str:
    if not items:
        return (
            "📰 <b>Новости</b>\n\n"
            "Новостной поток подключён только при наличии <code>CRYPTOPANIC_API_KEY</code>.\n\n"
            "Настройте ключ в Railway, и здесь будут свежие заголовки по рынкам."
        )
    lines: list[str] = ["📰 <b>Новости рынка</b>", LINE]
    for item in items[:8]:
        title = html.escape((item.get("title") or "")[:180])
        url = html.escape(((item.get("url") or "").replace("<", "&lt;").replace(">", "&gt;")))
        source = html.escape((item.get("source") or "CryptoPanic"))
        lines.append(f"• <b>{title}</b>")
        if url:
            lines.append(f"  <i>{source}</i> · <a href=\"{url}\">читать</a>")
    lines.append(LINE)
    lines.append("<i>Источник: CryptoPanic (реальные публикации).</i>")
    return "\n".join(lines)


def format_history(rows: list[dict]) -> str:
    if not rows:
        return "📚 <b>История идей</b>\n\nПока нет сохранённых сигналов. Запустите скан или анализ — они появятся здесь."
    lines: list[str] = ["📚 <b>История идей</b>", LINE]
    for row in rows[:10]:
        d = "🟢 L" if row.get("direction") == "LONG" else "🔴 S"
        lines.append(
            f"{d} <b>{html.escape(row.get('symbol', ''))}</b> · {row.get('timeframe', '')} · "
            f"вход {format_price(row.get('entry_low', 0))} · стоп {format_price(row.get('stop_loss', 0))} · "
            f"уверенность {row.get('confidence', 0)}%"
        )
    lines.append(LINE)
    lines.append("<i>Последние идеи из локальной БД.</i>")
    return "\n".join(lines)


def format_risk(size: float, stop_pct: float) -> str:
    risk_amount = size * stop_pct / 100.0
    rr = [1.5, 2.5, 4.0]
    lines: list[str] = [
        "🧮 <b>Риск-калькулятор</b>", LINE,
        f"💼 Размер позиции: <code>${size:,.2f}</code>",
        f"🛑 Стоп: <code>{stop_pct:.2f}%</code>",
        f"💰 Риск на сделку: <code>${risk_amount:,.2f}</code>",
        LINE,
        "🎯 Прибыль при целях:",
    ]
    for r in rr:
        lines.append(f"• R:R 1:{r:.1f} → <code>${risk_amount * r:,.2f}</code>")
    lines.append(LINE)
    lines.append("<i>Проверяйте риск на сделку: для одного плеча обычно 1–3% депозита.</i>")
    return "\n".join(lines)


def format_alert_saved(chat_id: int, symbol: str, above: float | None, below: float | None) -> str:
    conds = []
    if above:
        conds.append(f"выше <code>{format_price(above)}</code>")
    if below:
        conds.append(f"ниже <code>{format_price(below)}</code>")
    if not conds:
        conds = ["выше/ниже заданной цены"]
    return (
        f"🔔 <b>Алерт создан</b>\n\n"
        f"Монета: <code>{html.escape(symbol)}</code>\n"
        f"Условие: {', '.join(conds)}\n\n"
        "Бот будет проверять цену по реальным биржам и уведомит вас, когда условие сработает."
    )


def format_data_status(status: dict) -> str:
    lines: list[str] = ["🩺 <b>Доступ к реальным данным</b>"]
    sources: dict[str, Any] = status.get("sources", {})
    lines.append(LINE)
    lines.append("<b>Биржи (основной источник):</b>")
    exchanges = sources.get("exchanges", {})
    for name, st in exchanges.items() if isinstance(exchanges, dict) else []:
        ok = bool(st.get("ok", False))
        marks = "🟢" if ok else "🔴"
        detail = html.escape(str(st.get("detail", ""))[:180])
        lines.append(f"{marks} <b>{name}</b>: {'доступно' if ok else 'недоступно'} · {detail}")

    lines.append(LINE)
    lines.append("<b>Опциональные источники:</b>")
    for key in ("coinglass", "cryptopanic", "fear_greed"):
        st = sources.get(key)
        if not isinstance(st, dict):
            continue
        ok = bool(st.get("ok", False))
        marks = "🟢" if ok else "🟡"
        detail = html.escape(str(st.get("detail", ""))[:120])
        lines.append(f"{marks} <b>{key}</b>: {'доступен' if ok else 'выключен/недоступен'} · {detail}")

    lines.append(LINE)
    any_ok = bool(status.get("any_exchange_ok"))
    all_ok = bool(status.get("all_exchanges_ok"))
    if all_ok:
        lines.append("✅ <b>Все биржи доступны</b> — бот работает на реальных котировках.")
    elif any_ok:
        lines.append("⚠️ <b>Часть бирж доступна</b> — бот будет использовать доступный источник.")
    else:
        lines.append("🔴 <b>Биржи недоступны</b> — проверьте исходящий интернет/домены на Railway.")
    lines.append(LINE)
    lines.append("<i>Это live-проверка и она не использует mock-данные.</i>")
    return "\n".join(lines)


def format_alert_trigger(alert: dict, price: float, reason: str) -> str:
    conds = []
    if alert.get("target_above"):
        conds.append(f"> {format_price(alert['target_above'])}")
    if alert.get("target_below"):
        conds.append(f"< {format_price(alert['target_below'])}")
    return (
        f"🔔 <b>Ценовой алерт сработал!</b>\n\n"
        f"Монета: <code>{html.escape(alert.get('symbol', ''))}</code>\n"
        f"Текущая цена: <code>{format_price(price)}</code>\n"
        f"Условие: {', '.join(conds) or reason}\n\n"
        "Проверьте сетап перед действием."
    )
