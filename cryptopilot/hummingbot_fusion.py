from __future__ import annotations

from dataclasses import dataclass

from cryptopilot.models import Side, Ticker


@dataclass(frozen=True, slots=True)
class HummingbotFusion:
    """Hummingbot-inspired market-microstructure layer.

    This is not the Hummingbot codebase. It applies the useful V2 idea of a
    shared market-data layer (trades + order book + execution quality) to
    Scaner's existing ticker context. It never places orders.
    """

    score_delta: float
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    data_points: int


def evaluate(side: Side, ticker: Ticker) -> HummingbotFusion:
    if side is Side.NO_TRADE:
        return HummingbotFusion(0.0, (), (), 0)

    delta = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    points = 0

    # Hummingbot's MarketDataProvider exposes trades/order-book data to
    # strategy logic. Scaner already collects equivalent Bybit context.
    if ticker.taker_buy_ratio is not None:
        points += 1
        directional = (
            ticker.taker_buy_ratio if side is Side.LONG else 1.0 - ticker.taker_buy_ratio
        )
        if 0.54 <= directional <= 0.72:
            delta += 4.0
            reasons.append(f"HB flow: taker {directional:.0%} supports direction")
        elif directional < 0.40:
            delta -= 5.0
            risks.append(f"HB flow: taker {directional:.0%} strongly opposes direction")
        elif directional > 0.82:
            delta -= 2.0
            risks.append(f"HB flow: taker {directional:.0%} is already extreme")

    if ticker.orderbook_imbalance is not None:
        points += 1
        directional = (
            ticker.orderbook_imbalance if side is Side.LONG else -ticker.orderbook_imbalance
        )
        if directional >= 0.10:
            delta += 3.0
            reasons.append(f"HB book: imbalance {directional:+.0%} supports direction")
        elif directional <= -0.15:
            delta -= 4.0
            risks.append(f"HB book: imbalance {directional:+.0%} opposes direction")

    # Spot/perp basis is useful as a confirmation/quality filter, not a signal
    # by itself. A large adverse basis can indicate crowded positioning.
    if ticker.spot_perp_basis_bps is not None:
        points += 1
        adverse = ticker.spot_perp_basis_bps if side is Side.LONG else -ticker.spot_perp_basis_bps
        if adverse > 35:
            delta -= 4.0
            risks.append(
                f"HB basis: perp premium {ticker.spot_perp_basis_bps:+.0f} bps is stretched"
            )
        elif adverse < -20:
            delta += 2.0
            reasons.append(f"HB basis: {ticker.spot_perp_basis_bps:+.0f} bps leaves room")

    # Block-trade flow is optional on Bybit. Treat it as a weak confirmation.
    if ticker.spot_block_trade_buy_ratio is not None and ticker.spot_block_trade_notional:
        points += 1
        directional = (
            ticker.spot_block_trade_buy_ratio
            if side is Side.LONG
            else 1.0 - ticker.spot_block_trade_buy_ratio
        )
        if directional >= 0.60:
            delta += 2.0
            reasons.append(f"HB spot blocks: {directional:.0%} aligned")
        elif directional <= 0.35:
            delta -= 2.0
            risks.append(f"HB spot blocks: {directional:.0%} against direction")

    # Execution quality: Hummingbot's executors are designed to manage orders
    # as a lifecycle. For Scaner this becomes a signal-quality gate: a wide
    # spread reduces confidence instead of generating a false entry.
    if ticker.spread_bps > 10:
        points += 1
        delta -= min(5.0, (ticker.spread_bps - 10.0) * 0.5)
        risks.append(f"HB execution: spread {ticker.spread_bps:.1f} bps")

    return HummingbotFusion(
        score_delta=max(-10.0, min(10.0, delta)),
        reasons=tuple(reasons[:4]),
        risks=tuple(risks[:4]),
        data_points=points,
    )
