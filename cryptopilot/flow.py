from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from cryptopilot.models import Side


@dataclass(frozen=True, slots=True)
class TradePoint:
    ts_ms: int
    price: float
    signed_notional: float


@dataclass(frozen=True, slots=True)
class OIPoint:
    ts_ms: int
    value_usdt: float


@dataclass(frozen=True, slots=True)
class FlowSnapshot:
    symbol: str
    created_ms: int
    age_ms: int
    price: float
    notional_60s: float
    notional_prev_60s: float
    notional_5m: float
    delta_60s_usdt: float
    cvd_5m_usdt: float
    delta_ratio_60s: float
    cvd_ratio_5m: float
    volume_burst_ratio: float | None
    price_change_60s_pct: float | None
    oi_change_2m_pct: float | None
    oi_change_prev_2m_pct: float | None
    oi_acceleration_pct_per_min: float | None
    absorption: str | None
    trade_count_60s: int

    @property
    def fresh(self) -> bool:
        return 0 <= self.age_ms <= 10_000


@dataclass(frozen=True, slots=True)
class FlowPressureEvent:
    symbol: str
    bias: Side
    score: int
    price: float
    trigger_price: float
    created_ms: int
    event_type: str
    snapshot: FlowSnapshot
    reasons: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return f"FLOW:{self.symbol}:{self.bias.value}:{self.event_type}"


class FlowTracker:
    """Rolling public-trade CVD proxy and ticker OI monitor.

    Bybit publicTrade reports the taker side. Signed taker notional is therefore a useful
    public CVD proxy, but it is not a full exchange footprint and does not identify traders.
    """

    def __init__(self) -> None:
        self._trades: dict[str, deque[TradePoint]] = defaultdict(lambda: deque(maxlen=20_000))
        self._oi: dict[str, deque[OIPoint]] = defaultdict(lambda: deque(maxlen=1_200))
        self._last_price: dict[str, float] = {}
        self._last_oi_value: dict[str, float] = {}
        self._last_oi_sample_ms: dict[str, int] = {}
        self._last_event_ms: dict[str, int] = {}

    def add_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        size: float,
        ts_ms: int,
    ) -> None:
        if (
            ts_ms <= 0
            or not math.isfinite(price)
            or not math.isfinite(size)
            or price <= 0
            or size <= 0
        ):
            return
        normalized = symbol.upper()
        signed = price * size * (1.0 if side == "Buy" else -1.0 if side == "Sell" else 0.0)
        if signed == 0:
            return
        points = self._trades[normalized]
        if points and ts_ms < points[-1].ts_ms:
            return
        points.append(TradePoint(ts_ms, price, signed))
        self._last_price[normalized] = price
        self._trim_trades(normalized, ts_ms)

    def add_ticker(
        self,
        symbol: str,
        ts_ms: int,
        *,
        last_price: float | None = None,
        open_interest: float | None = None,
        open_interest_value: float | None = None,
    ) -> None:
        if ts_ms <= 0:
            return
        normalized = symbol.upper()
        if last_price is not None and math.isfinite(last_price) and last_price > 0:
            self._last_price[normalized] = last_price

        value = open_interest_value
        if value is None and open_interest is not None:
            price = self._last_price.get(normalized)
            if price is not None:
                value = open_interest * price
        if value is None or not math.isfinite(value) or value <= 0:
            return

        self._last_oi_value[normalized] = value
        last_sample = self._last_oi_sample_ms.get(normalized, 0)
        if ts_ms - last_sample < 1_000:
            return
        points = self._oi[normalized]
        if points and ts_ms < points[-1].ts_ms:
            return
        points.append(OIPoint(ts_ms, value))
        self._last_oi_sample_ms[normalized] = ts_ms
        self._trim_oi(normalized, ts_ms)

    def snapshot(self, symbol: str, now_ms: int | None = None) -> FlowSnapshot | None:
        normalized = symbol.upper()
        trades = self._trades.get(normalized)
        if not trades:
            return None
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self._trim_trades(normalized, current_ms)
        self._trim_oi(normalized, current_ms)
        trades = self._trades.get(normalized)
        if not trades:
            return None

        latest = trades[-1]
        current_60 = [p for p in trades if current_ms - 60_000 < p.ts_ms <= current_ms]
        previous_60 = [
            p for p in trades if current_ms - 120_000 < p.ts_ms <= current_ms - 60_000
        ]
        current_5m = [p for p in trades if current_ms - 300_000 < p.ts_ms <= current_ms]
        if not current_60:
            return None

        total_60 = sum(abs(p.signed_notional) for p in current_60)
        total_prev_60 = sum(abs(p.signed_notional) for p in previous_60)
        total_5m = sum(abs(p.signed_notional) for p in current_5m)
        delta_60 = sum(p.signed_notional for p in current_60)
        cvd_5m = sum(p.signed_notional for p in current_5m)
        delta_ratio = delta_60 / total_60 if total_60 > 0 else 0.0
        cvd_ratio = cvd_5m / total_5m if total_5m > 0 else 0.0
        burst = total_60 / total_prev_60 if total_prev_60 > 0 else None
        price_change = _price_change(current_60)

        oi_points = list(self._oi.get(normalized, ()))
        oi_current = _window_change(oi_points, current_ms - 120_000, current_ms)
        oi_previous = _window_change(oi_points, current_ms - 240_000, current_ms - 120_000)
        acceleration = None
        if oi_current is not None and oi_previous is not None:
            acceleration = (oi_current - oi_previous) / 2.0

        absorption = None
        if total_60 > 0 and price_change is not None and abs(price_change) <= 0.12:
            if delta_ratio <= -0.18 and price_change >= -0.08:
                absorption = "BUY_ABSORPTION"
            elif delta_ratio >= 0.18 and price_change <= 0.08:
                absorption = "SELL_ABSORPTION"

        return FlowSnapshot(
            symbol=normalized,
            created_ms=current_ms,
            age_ms=max(0, current_ms - latest.ts_ms),
            price=latest.price,
            notional_60s=total_60,
            notional_prev_60s=total_prev_60,
            notional_5m=total_5m,
            delta_60s_usdt=delta_60,
            cvd_5m_usdt=cvd_5m,
            delta_ratio_60s=delta_ratio,
            cvd_ratio_5m=cvd_ratio,
            volume_burst_ratio=burst,
            price_change_60s_pct=price_change,
            oi_change_2m_pct=oi_current,
            oi_change_prev_2m_pct=oi_previous,
            oi_acceleration_pct_per_min=acceleration,
            absorption=absorption,
            trade_count_60s=len(current_60),
        )

    def pressure_event(
        self,
        symbol: str,
        bias: Side,
        trigger_price: float,
        *,
        min_notional_60s: float,
        delta_threshold: float,
        burst_threshold: float,
        min_oi_change_pct: float,
        min_score: int,
        cooldown_seconds: int = 180,
        now_ms: int | None = None,
    ) -> FlowPressureEvent | None:
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        snapshot = self.snapshot(symbol, current_ms)
        if snapshot is None or not snapshot.fresh or snapshot.notional_60s < min_notional_60s:
            return None
        if trigger_price <= 0 or snapshot.price <= 0:
            return None

        bullish = bias is Side.LONG
        # This event is specifically meant to arrive before the structural trigger.
        if bullish and snapshot.price >= trigger_price:
            return None
        if not bullish and snapshot.price <= trigger_price:
            return None
        distance_pct = abs(snapshot.price / trigger_price - 1) * 100
        if distance_pct > 1.5:
            return None

        direction = 1.0 if bullish else -1.0
        directional_delta = direction * snapshot.delta_ratio_60s
        directional_cvd = direction * snapshot.cvd_ratio_5m
        matching_absorption = (
            snapshot.absorption == "BUY_ABSORPTION"
            if bullish
            else snapshot.absorption == "SELL_ABSORPTION"
        )
        opposing_absorption = (
            snapshot.absorption == "SELL_ABSORPTION"
            if bullish
            else snapshot.absorption == "BUY_ABSORPTION"
        )

        score = 0
        reasons: list[str] = []
        event_type = "FLOW_BUILDUP"

        if directional_delta >= delta_threshold:
            score += 25
            reasons.append(
                f"60s taker delta {directional_delta:+.0%} в сторону {bias.value}"
            )
        elif directional_delta <= -delta_threshold:
            score -= 12

        if directional_cvd >= 0.08:
            score += 12
            reasons.append(f"5m CVD proxy {directional_cvd:+.0%} поддерживает направление")
        elif directional_cvd <= -0.12:
            score -= 8

        burst = snapshot.volume_burst_ratio
        if burst is not None and burst >= burst_threshold:
            score += 15
            reasons.append(f"Поток сделок ускорился: {burst:.2f}× к прошлой минуте")

        oi_change = snapshot.oi_change_2m_pct
        if oi_change is not None and oi_change >= min_oi_change_pct:
            score += 18
            reasons.append(f"OI +{oi_change:.2f}% за ~2 мин: новые позиции нарастают")
        elif oi_change is not None and oi_change <= -min_oi_change_pct:
            score -= 10

        acceleration = snapshot.oi_acceleration_pct_per_min
        if acceleration is not None and acceleration >= 0.03:
            score += 10
            reasons.append(f"OI ускоряется: {acceleration:+.3f}%/мин")
        elif acceleration is not None and acceleration <= -0.05:
            score -= 5

        if matching_absorption:
            score += 24
            event_type = "ABSORPTION"
            reasons.append(
                "Агрессивный поток поглощается, а цена удерживается — возможен пассивный набор"
            )
        elif opposing_absorption:
            score -= 15

        if distance_pct <= 0.6:
            score += 10
            reasons.append(f"До структурного trigger осталось {distance_pct:.2f}%")
        elif distance_pct <= 1.0:
            score += 6

        score = max(0, min(score, 100))
        key = f"{symbol.upper()}:{bias.value}:{event_type}"
        previous = self._last_event_ms.get(key, 0)
        if score < min_score or current_ms - previous < cooldown_seconds * 1000:
            return None
        self._last_event_ms[key] = current_ms
        return FlowPressureEvent(
            symbol=symbol.upper(),
            bias=bias,
            score=score,
            price=snapshot.price,
            trigger_price=trigger_price,
            created_ms=current_ms,
            event_type=event_type,
            snapshot=snapshot,
            reasons=tuple(reasons[:6]),
        )

    def _trim_trades(self, symbol: str, now_ms: int) -> None:
        points = self._trades.get(symbol)
        if not points:
            return
        cutoff = now_ms - 360_000
        while points and points[0].ts_ms < cutoff:
            points.popleft()

    def _trim_oi(self, symbol: str, now_ms: int) -> None:
        points = self._oi.get(symbol)
        if not points:
            return
        cutoff = now_ms - 600_000
        while points and points[0].ts_ms < cutoff:
            points.popleft()


def _price_change(points: list[TradePoint]) -> float | None:
    if len(points) < 2 or points[0].price <= 0:
        return None
    return (points[-1].price / points[0].price - 1) * 100


def _window_change(points: list[OIPoint], start_ms: int, end_ms: int) -> float | None:
    values = [p for p in points if start_ms <= p.ts_ms <= end_ms]
    if len(values) < 2 or values[0].value_usdt <= 0:
        return None
    duration = values[-1].ts_ms - values[0].ts_ms
    if duration < 30_000:
        return None
    return (values[-1].value_usdt / values[0].value_usdt - 1) * 100
