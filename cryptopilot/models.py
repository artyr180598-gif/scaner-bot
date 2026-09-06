from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


CURRENT_STRATEGY_VERSION = "premove-3.6"


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
    open_interest_change_pct: float | None = None
    taker_buy_ratio: float | None = None
    orderbook_imbalance: float | None = None
    long_short_ratio: float | None = None
    spot_last: float | None = None
    spot_taker_buy_ratio: float | None = None
    spot_orderbook_imbalance: float | None = None
    spot_block_trade_buy_ratio: float | None = None
    spot_block_trade_notional: float | None = None
    spot_perp_basis_bps: float | None = None

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
    plus_di14: float
    minus_di14: float
    dmi_spread: float
    macd_hist: float
    bb_position: float
    bb_width_pct: float
    bb_width_regime_ratio: float
    volume_z: float
    efficiency_ratio20: float
    ema_gap_atr: float
    atr_regime_ratio: float
    breakout_up: bool
    breakout_down: bool
    range_high20: float
    range_low20: float
    range_position20: float
    return_20_pct: float
    keltner_squeeze_ratio: float
    squeeze_bars: int
    choppiness14: float
    cmf20: float
    relative_volume20: float
    vwap_distance_atr: float
    supertrend_direction: int
    supertrend_distance_atr: float


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
    scale_entries: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale_allocations_pct: tuple[int, int, int] = (50, 30, 20)
    recommended_leverage: int = 1
    max_leverage: int = 3
    holding_horizon: str = "1–72 часа"


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
    market_context: dict[str, float] = field(default_factory=dict)
    plan: TradePlan | None = None
    data_age_seconds: int = 0
    required_confidence: int = 0
    estimated_success_pct: float | None = None
    success_interval_low: float | None = None
    success_interval_high: float | None = None
    calibration_samples: int = 0
    recent_expectancy_r: float | None = None
    strategy_version: str = CURRENT_STRATEGY_VERSION

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


@dataclass(slots=True)
class EarlySetup:
    symbol: str
    exchange: str
    bias: Side
    readiness: int
    price: float
    trigger_price: float
    opposite_trigger_price: float
    invalidation_price: float
    stage: str
    regime: str
    created_at: datetime
    expires_at: datetime
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.bias is not Side.NO_TRADE and not self.blockers

    @property
    def fingerprint(self) -> str:
        return f"EARLY:{self.exchange}:{self.symbol}:{self.bias.value}"


@dataclass(frozen=True, slots=True)
class EarlyScanReport:
    exchange: str
    started_at: datetime
    finished_at: datetime
    universe_count: int
    analyzed_count: int
    setups: tuple[EarlySetup, ...]
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


@dataclass(frozen=True, slots=True)
class PaperTrade:
    id: int
    symbol: str
    exchange: str
    side: Side
    confidence: int
    regime: str
    created_at: datetime
    entry_expires_at: datetime
    exit_expires_at: datetime
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit: float
    status: str
    entry_price: float | None = None
    entry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CalibrationStats:
    sample_size: int
    wins: int
    losses: int
    win_rate: float
    interval_low: float
    interval_high: float
    expectancy_r: float
    profit_factor: float
