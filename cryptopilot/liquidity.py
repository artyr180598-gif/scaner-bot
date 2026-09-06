from __future__ import annotations

import math
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiquiditySnapshot:
    symbol: str
    created_ms: int
    spread_bps: float | None
    imbalance_top10: float | None
    bid_wall_ratio: float | None
    ask_wall_ratio: float | None
    bid_wall_price: float | None
    ask_wall_price: float | None
    bid_wall_persistence_seconds: float
    ask_wall_persistence_seconds: float
    bid_replenishment_usdt_60s: float
    ask_replenishment_usdt_60s: float
    long_liquidation_usdt_60s: float
    short_liquidation_usdt_60s: float


@dataclass(frozen=True, slots=True)
class _Replenishment:
    ts_ms: int
    side: str
    notional: float


@dataclass(frozen=True, slots=True)
class _Liquidation:
    ts_ms: int
    position_side: str
    notional: float


class LiquidityTracker:
    """Persistent public order-book and liquidation intelligence.

    The tracker never treats a visible wall as proof of a real large buyer/seller. It only
    rewards walls that persist and replenish over time, while liquidation flow is used to
    distinguish new participation from an already-running squeeze.
    """

    def __init__(self) -> None:
        self._bids: dict[str, dict[float, float]] = defaultdict(dict)
        self._asks: dict[str, dict[float, float]] = defaultdict(dict)
        self._replenishment: dict[str, deque[_Replenishment]] = defaultdict(
            lambda: deque(maxlen=20_000)
        )
        self._liquidations: dict[str, deque[_Liquidation]] = defaultdict(
            lambda: deque(maxlen=10_000)
        )
        self._wall_state: dict[tuple[str, str], tuple[float, int]] = {}
        self._last_wall_eval_ms: dict[str, int] = {}

    def update_orderbook(
        self,
        symbol: str,
        message_type: str,
        bids: list[list[str]],
        asks: list[list[str]],
        ts_ms: int,
    ) -> None:
        if ts_ms <= 0:
            return
        normalized = symbol.upper()
        if message_type == "snapshot":
            self._bids[normalized].clear()
            self._asks[normalized].clear()

        self._apply_levels(normalized, "bid", self._bids[normalized], bids, ts_ms)
        self._apply_levels(normalized, "ask", self._asks[normalized], asks, ts_ms)
        self._trim(normalized, ts_ms)

        last_eval = self._last_wall_eval_ms.get(normalized, 0)
        if ts_ms - last_eval >= 500:
            self._refresh_wall_state(normalized, ts_ms)
            self._last_wall_eval_ms[normalized] = ts_ms

    def add_liquidation(
        self,
        symbol: str,
        position_side: str,
        price: float,
        size: float,
        ts_ms: int,
    ) -> None:
        if (
            ts_ms <= 0
            or price <= 0
            or size <= 0
            or not math.isfinite(price)
            or not math.isfinite(size)
        ):
            return
        side = position_side.upper()
        if side not in {"LONG", "SHORT"}:
            return
        normalized = symbol.upper()
        self._liquidations[normalized].append(
            _Liquidation(ts_ms, side, price * size)
        )
        self._trim(normalized, ts_ms)

    def snapshot(self, symbol: str, now_ms: int) -> LiquiditySnapshot | None:
        normalized = symbol.upper()
        bids = self._bids.get(normalized)
        asks = self._asks.get(normalized)
        if not bids or not asks:
            return None
        self._trim(normalized, now_ms)
        self._refresh_wall_state(normalized, now_ms)

        bid_levels = sorted(bids.items(), reverse=True)[:10]
        ask_levels = sorted(asks.items())[:10]
        if not bid_levels or not ask_levels:
            return None

        best_bid = bid_levels[0][0]
        best_ask = ask_levels[0][0]
        mid = (best_bid + best_ask) / 2
        spread_bps = (best_ask - best_bid) / mid * 10_000 if mid > 0 else None

        bid_notional = [price * size for price, size in bid_levels]
        ask_notional = [price * size for price, size in ask_levels]
        bid_total = sum(bid_notional)
        ask_total = sum(ask_notional)
        imbalance = (
            (bid_total - ask_total) / (bid_total + ask_total)
            if bid_total + ask_total > 0
            else None
        )

        bid_wall_ratio, bid_wall_price = _wall_ratio(bid_levels)
        ask_wall_ratio, ask_wall_price = _wall_ratio(ask_levels)
        bid_persistence = self._wall_persistence(
            normalized, "bid", bid_wall_price, now_ms
        )
        ask_persistence = self._wall_persistence(
            normalized, "ask", ask_wall_price, now_ms
        )

        replenishments = self._replenishment.get(normalized, ())
        bid_replenishment = sum(
            item.notional
            for item in replenishments
            if item.side == "bid" and now_ms - 60_000 < item.ts_ms <= now_ms
        )
        ask_replenishment = sum(
            item.notional
            for item in replenishments
            if item.side == "ask" and now_ms - 60_000 < item.ts_ms <= now_ms
        )

        liquidations = self._liquidations.get(normalized, ())
        long_liquidation = sum(
            item.notional
            for item in liquidations
            if item.position_side == "LONG" and now_ms - 60_000 < item.ts_ms <= now_ms
        )
        short_liquidation = sum(
            item.notional
            for item in liquidations
            if item.position_side == "SHORT" and now_ms - 60_000 < item.ts_ms <= now_ms
        )

        return LiquiditySnapshot(
            symbol=normalized,
            created_ms=now_ms,
            spread_bps=spread_bps,
            imbalance_top10=imbalance,
            bid_wall_ratio=bid_wall_ratio,
            ask_wall_ratio=ask_wall_ratio,
            bid_wall_price=bid_wall_price,
            ask_wall_price=ask_wall_price,
            bid_wall_persistence_seconds=bid_persistence,
            ask_wall_persistence_seconds=ask_persistence,
            bid_replenishment_usdt_60s=bid_replenishment,
            ask_replenishment_usdt_60s=ask_replenishment,
            long_liquidation_usdt_60s=long_liquidation,
            short_liquidation_usdt_60s=short_liquidation,
        )

    def _apply_levels(
        self,
        symbol: str,
        side: str,
        book: dict[float, float],
        rows: list[list[str]],
        ts_ms: int,
    ) -> None:
        for row in rows:
            if len(row) < 2:
                continue
            try:
                price = float(row[0])
                size = float(row[1])
            except (TypeError, ValueError):
                continue
            if price <= 0 or size < 0 or not math.isfinite(price) or not math.isfinite(size):
                continue
            previous = book.get(price, 0.0)
            if size == 0:
                book.pop(price, None)
                continue
            book[price] = size
            if previous > 0 and size > previous:
                self._replenishment[symbol].append(
                    _Replenishment(ts_ms, side, (size - previous) * price)
                )

    def _refresh_wall_state(self, symbol: str, now_ms: int) -> None:
        for side, book in (("bid", self._bids.get(symbol)), ("ask", self._asks.get(symbol))):
            if not book:
                self._wall_state.pop((symbol, side), None)
                continue
            levels = (
                sorted(book.items(), reverse=True)[:10]
                if side == "bid"
                else sorted(book.items())[:10]
            )
            ratio, wall_price = _wall_ratio(levels)
            key = (symbol, side)
            if wall_price is None or ratio is None or ratio < 2.0:
                self._wall_state.pop(key, None)
                continue
            previous = self._wall_state.get(key)
            if previous is None or abs(previous[0] / wall_price - 1) > 0.0005:
                self._wall_state[key] = (wall_price, now_ms)

    def _wall_persistence(
        self,
        symbol: str,
        side: str,
        wall_price: float | None,
        now_ms: int,
    ) -> float:
        if wall_price is None:
            return 0.0
        state = self._wall_state.get((symbol, side))
        if state is None or abs(state[0] / wall_price - 1) > 0.0005:
            return 0.0
        return max(0.0, (now_ms - state[1]) / 1000)

    def _trim(self, symbol: str, now_ms: int) -> None:
        replenishments = self._replenishment.get(symbol)
        if replenishments:
            cutoff = now_ms - 120_000
            while replenishments and replenishments[0].ts_ms < cutoff:
                replenishments.popleft()
        liquidations = self._liquidations.get(symbol)
        if liquidations:
            cutoff = now_ms - 300_000
            while liquidations and liquidations[0].ts_ms < cutoff:
                liquidations.popleft()


def _wall_ratio(levels: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    notionals = [price * size for price, size in levels if price > 0 and size > 0]
    if len(notionals) < 3:
        return None, None
    median = statistics.median(notionals)
    if median <= 0:
        return None, None
    max_index = max(range(len(notionals)), key=notionals.__getitem__)
    return notionals[max_index] / median, levels[max_index][0]
