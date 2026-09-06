from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptopilot.config import Settings
from cryptopilot.economics import net_reward_risk
from cryptopilot.models import Candle, FeatureSet, Side, TradePlan


@dataclass(frozen=True, slots=True)
class PrimePlanResult:
    plan: TradePlan | None
    blockers: tuple[str, ...] = ()


def build_prime_plan(
    side: Side,
    price: float,
    trigger_price: float,
    candles_5m: list[Candle],
    feature15: FeatureSet,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> PrimePlanResult:
    """Build a pre-breakout plan without chasing the structural trigger.

    The plan is intentionally conservative: entry must stay before the trigger, stop size is
    bounded, and TP2 must still offer enough net R/R after the configured execution cost.
    """

    if side is Side.NO_TRADE or price <= 0 or trigger_price <= 0:
        return PrimePlanResult(None, ("Некорректные данные для PRIME-плана",))

    now = now or datetime.now(UTC)
    atr = max(feature15.atr14, price * 0.001)
    trigger_buffer = max(0.06 * atr, price * 0.00035)

    if side is Side.LONG:
        entry_low = price - 0.20 * atr
        entry_high = min(price + 0.08 * atr, trigger_price - trigger_buffer)
        if entry_high <= entry_low:
            return PrimePlanResult(
                None,
                ("До LONG-trigger уже слишком мало пространства для ранней зоны входа",),
            )
    else:
        entry_low = max(price - 0.08 * atr, trigger_price + trigger_buffer)
        entry_high = price + 0.20 * atr
        if entry_high <= entry_low:
            return PrimePlanResult(
                None,
                ("До SHORT-trigger уже слишком мало пространства для ранней зоны входа",),
            )

    entry_mid = (entry_low + entry_high) / 2
    recent = candles_5m[-18:] if candles_5m else []
    if side is Side.LONG:
        swing = min((bar.low for bar in recent), default=price - 1.2 * atr)
        technical_stop = max(
            swing - 0.12 * atr,
            feature15.ema20 - 0.65 * atr,
        )
        stop = min(technical_stop, entry_low - 0.45 * atr)
        minimum_stop = entry_mid * (1 - 0.0035)
        if stop > minimum_stop:
            stop = minimum_stop
        risk = entry_mid - stop
    else:
        swing = max((bar.high for bar in recent), default=price + 1.2 * atr)
        technical_stop = min(
            swing + 0.12 * atr,
            feature15.ema20 + 0.65 * atr,
        )
        stop = max(technical_stop, entry_high + 0.45 * atr)
        minimum_stop = entry_mid * (1 + 0.0035)
        if stop < minimum_stop:
            stop = minimum_stop
        risk = stop - entry_mid

    if risk <= 0:
        return PrimePlanResult(None, ("Не удалось построить корректный технический стоп",))

    stop_pct = risk / entry_mid * 100
    if stop_pct > settings.prime_max_stop_pct:
        return PrimePlanResult(
            None,
            (
                f"Технический стоп PRIME {stop_pct:.2f}% слишком широкий; "
                f"максимум {settings.prime_max_stop_pct:.2f}%",
            ),
        )

    structure_width = max(
        abs(feature15.range_high20 - feature15.range_low20),
        risk * 1.5,
    )
    if side is Side.LONG:
        tp1 = max(entry_mid + risk, trigger_price + 0.15 * risk)
        tp2 = max(
            entry_mid + settings.prime_min_plan_rr * risk,
            trigger_price + 0.30 * structure_width,
        )
        tp3 = max(
            entry_mid + 2.8 * risk,
            trigger_price + 0.60 * structure_width,
        )
        # Do not invent absurd targets from a stale wide range.
        tp2 = min(tp2, entry_mid + 3.2 * risk)
        tp3 = min(tp3, entry_mid + 4.2 * risk)
    else:
        tp1 = min(entry_mid - risk, trigger_price - 0.15 * risk)
        tp2 = min(
            entry_mid - settings.prime_min_plan_rr * risk,
            trigger_price - 0.30 * structure_width,
        )
        tp3 = min(
            entry_mid - 2.8 * risk,
            trigger_price - 0.60 * structure_width,
        )
        tp2 = max(tp2, entry_mid - 3.2 * risk)
        tp3 = max(tp3, entry_mid - 4.2 * risk)

    worst_entry = entry_high if side is Side.LONG else entry_low
    worst_risk = (
        worst_entry - stop
        if side is Side.LONG
        else stop - worst_entry
    )
    # Entry is a zone, so validate TP2 from the least favorable fill, not the midpoint.
    # Raise the target only within a bounded 4R envelope; otherwise reject the plan.
    net_rr = net_reward_risk(
        side is Side.LONG,
        worst_entry,
        stop,
        tp2,
        settings.paper_one_way_cost_bps,
    )
    maximum_tp2 = (
        worst_entry + 4.0 * worst_risk
        if side is Side.LONG
        else worst_entry - 4.0 * worst_risk
    )
    step = 0.10 * worst_risk
    while net_rr < settings.prime_min_plan_rr and step > 0:
        next_tp2 = tp2 + step if side is Side.LONG else tp2 - step
        if (
            side is Side.LONG and next_tp2 > maximum_tp2
        ) or (
            side is Side.SHORT and next_tp2 < maximum_tp2
        ):
            break
        tp2 = next_tp2
        net_rr = net_reward_risk(
            side is Side.LONG,
            worst_entry,
            stop,
            tp2,
            settings.paper_one_way_cost_bps,
        )

    if net_rr < settings.prime_min_plan_rr:
        return PrimePlanResult(
            None,
            (
                f"PRIME TP2 даёт только R/R {net_rr:.2f} после издержек; "
                f"нужно {settings.prime_min_plan_rr:.2f}+",
            ),
        )

    if side is Side.LONG:
        tp3 = max(tp3, tp2 + 0.8 * worst_risk)
    else:
        tp3 = min(tp3, tp2 - 0.8 * worst_risk)

    risk_amount = (
        settings.account_equity_usdt
        * settings.risk_per_trade_pct
        / 100
        * settings.prime_risk_multiplier
    )
    theoretical_notional = risk_amount / max(risk / entry_mid, 1e-12)
    cap = settings.account_equity_usdt * settings.max_position_pct / 100
    leverage = min(settings.preferred_leverage, settings.prime_max_leverage)
    cap *= leverage
    notional = min(theoretical_notional, cap)

    invalidation = (
        "Отменить лимит, если цена пробила стоп/сломала 15m структуру "
        "или не активировала сценарий до срока. После ухода за trigger без входа не догонять."
    )
    return PrimePlanResult(
        TradePlan(
            entry_low=float(entry_low),
            entry_high=float(entry_high),
            stop_loss=float(stop),
            take_profit_1=float(tp1),
            take_profit_2=float(tp2),
            take_profit_3=float(tp3),
            risk_reward_2=float(net_rr),
            invalidation=invalidation,
            expires_at=now + timedelta(minutes=settings.prime_entry_expiry_minutes),
            suggested_notional=float(notional),
            suggested_quantity=float(notional / entry_mid),
            risk_amount=float(min(risk_amount, notional * risk / entry_mid)),
            scale_entries=(float(entry_mid), 0.0, 0.0),
            scale_allocations_pct=(100, 0, 0),
            recommended_leverage=leverage,
            max_leverage=settings.prime_max_leverage,
            holding_horizon="до 72 часов; раньше при сломе структуры или истечении PRIME",
        )
    )
