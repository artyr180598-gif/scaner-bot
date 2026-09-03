"""HTML-formatted messages for Telegram (aiogram HTML parse mode)."""

from __future__ import annotations

import html

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
