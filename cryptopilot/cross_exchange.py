from __future__ import annotations

from dataclasses import dataclass

from cryptopilot.exchange import ExchangeClient
from cryptopilot.indicators import compute_features
from cryptopilot.models import Side, Ticker


@dataclass(frozen=True, slots=True)
class CrossExchangeSnapshot:
    exchange: str
    confirmations: int
    conflicts: int
    price_divergence_bps: float | None
    oi_change_pct: float | None
    taker_buy_ratio: float | None
    structure_15m: str
    structure_1h: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


async def verify_cross_exchange(
    exchange: ExchangeClient,
    ticker: Ticker,
    side: Side,
    primary_price: float,
) -> CrossExchangeSnapshot:
    enriched, candles15, candles1h = await _gather_context(exchange, ticker)
    f15 = compute_features(candles15)
    f1h = compute_features(candles1h)
    bullish = side is Side.LONG
    confirmations = 0
    conflicts = 0
    reasons: list[str] = []
    warnings: list[str] = []

    aligned15 = (
        f15.close > f15.ema20 > f15.ema50
        if bullish
        else f15.close < f15.ema20 < f15.ema50
    )
    if aligned15:
        confirmations += 1
        reasons.append(f"{exchange.name} 15m структура подтверждает {side.value}")
    else:
        opposite15 = (
            f15.close < f15.ema20 < f15.ema50
            if bullish
            else f15.close > f15.ema20 > f15.ema50
        )
        if opposite15:
            conflicts += 1
            warnings.append(f"{exchange.name} 15m структура против {side.value}")

    aligned1h = (
        f1h.close > f1h.ema50 and f1h.supertrend_direction > 0
        if bullish
        else f1h.close < f1h.ema50 and f1h.supertrend_direction < 0
    )
    if aligned1h:
        confirmations += 1
        reasons.append(f"{exchange.name} 1h подтверждает старший контекст")
    else:
        opposite1h = (
            f1h.close < f1h.ema50 and f1h.supertrend_direction < 0
            if bullish
            else f1h.close > f1h.ema50 and f1h.supertrend_direction > 0
        )
        if opposite1h:
            conflicts += 1
            warnings.append(f"{exchange.name} 1h направлен против сценария")

    if enriched.open_interest_change_pct is not None:
        if enriched.open_interest_change_pct > 0:
            confirmations += 1
            reasons.append(
                f"{exchange.name} OI {enriched.open_interest_change_pct:+.1f}%: "
                "новые позиции присутствуют"
            )
        elif enriched.open_interest_change_pct < -2:
            conflicts += 1
            warnings.append(
                f"{exchange.name} OI {enriched.open_interest_change_pct:+.1f}% сокращается"
            )

    taker = enriched.taker_buy_ratio
    if taker is not None:
        directional_taker = taker if bullish else 1 - taker
        if directional_taker >= 0.55:
            confirmations += 1
            reasons.append(
                f"{exchange.name} taker-flow {directional_taker:.0%} по сценарию"
            )
        elif directional_taker <= 0.43:
            conflicts += 1
            warnings.append(
                f"{exchange.name} taker-flow {directional_taker:.0%} против сценария"
            )

    divergence_bps = (
        abs(ticker.last / primary_price - 1) * 10_000
        if primary_price > 0 and ticker.last > 0
        else None
    )

    return CrossExchangeSnapshot(
        exchange=exchange.name,
        confirmations=confirmations,
        conflicts=conflicts,
        price_divergence_bps=divergence_bps,
        oi_change_pct=enriched.open_interest_change_pct,
        taker_buy_ratio=enriched.taker_buy_ratio,
        structure_15m=_label(f15),
        structure_1h=_label(f1h),
        reasons=tuple(reasons[:5]),
        warnings=tuple(warnings[:4]),
    )


async def _gather_context(exchange: ExchangeClient, ticker: Ticker):
    import asyncio

    return await asyncio.gather(
        exchange.enrich_ticker(ticker),
        exchange.candles(ticker.symbol, "15", 240),
        exchange.candles(ticker.symbol, "60", 240),
    )


def _label(feature) -> str:
    if feature.close > feature.ema20 > feature.ema50:
        return "BULL"
    if feature.close < feature.ema20 < feature.ema50:
        return "BEAR"
    return "MIXED"
