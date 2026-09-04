from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True, slots=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    turnover_24h: float
    volume_24h: float
    funding_rate: float = 0.0
    open_interest: float = 0.0

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2
        return ((self.ask - self.bid) / mid * 10_000) if mid > 0 else float("inf")


@dataclass(frozen=True, slots=True)
class FeatureSet:
    close: float
    ema20: float
    ema50: float
    ema200: float
    ema20_slope_pct: float
    rsi14: float
    atr14: float
    atr_pct: float
    adx14: float
    macd_hist: float
    bb_position: float
    volume_z: float
    breakout_up: bool
    breakout_down: bool
    return_20_pct: float


@dataclass(frozen=True, slots=True)
class TradePlan:
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    risk_reward_2: float
    invalidation: str
    expires_at: datetime
    suggested_notional: float
    suggested_quantity: float
    risk_amount: float


@dataclass(slots=True)
class Signal:
    symbol: str
    exchange: str
    side: Side
    confidence: int
    score: float
    regime: str
    price: float
    created_at: datetime
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    features: dict[str, FeatureSet] = field(default_factory=dict)
    plan: TradePlan | None = None
    data_age_seconds: int = 0

    @property
    def actionable(self) -> bool:
        return self.side is not Side.NO_TRADE and self.plan is not None

    @property
    def fingerprint(self) -> str:
        return f"{self.exchange}:{self.symbol}:{self.side}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["side"] = self.side.value
        payload["created_at"] = self.created_at.astimezone(UTC).isoformat()
        if self.plan:
            payload["plan"]["expires_at"] = self.plan.expires_at.astimezone(UTC).isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class ScanReport:
    exchange: str
    started_at: datetime
    finished_at: datetime
    universe_count: int
    analyzed_count: int
    signals: tuple[Signal, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    timeframe: str
    bars: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    expectancy_r: float
    profit_factor: float
    max_drawdown_r: float
    started_at: datetime
    finished_at: datetime
