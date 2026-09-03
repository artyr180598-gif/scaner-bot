"""Shared domain models for CryptoForge Pro."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_time_ms: int = 0
    close_time_ms: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "open_time_ms": self.open_time_ms,
            "close_time_ms": self.close_time_ms,
        }


@dataclass(slots=True)
class TickerInfo:
    symbol: str  # e.g. BTCUSDT
    base: str  # e.g. BTC
    quote: str  # e.g. USDT
    last_price: float
    high_24h: float = 0.0
    low_24h: float = 0.0
    volume_24h_base: float = 0.0
    volume_24h_quote: float = 0.0
    change_24h_pct: float = 0.0
    change_1h_pct: float = 0.0
    source: str = "binance"


@dataclass(slots=True)
class Derivatives:
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    open_interest_value_usd: Optional[float] = None
    long_short_ratio: Optional[float] = None
    liquidation_24h_usd: Optional[float] = None
    source: str = "bybit"

    @property
    def available(self) -> bool:
        return any(
            x is not None
            for x in (
                self.funding_rate,
                self.open_interest,
                self.open_interest_value_usd,
                self.long_short_ratio,
                self.liquidation_24h_usd,
            )
        )


@dataclass(slots=True)
class MarketData:
    symbol: str
    base: str
    quote: str
    price: float
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    ticker: TickerInfo | None = None
    derivatives: Derivatives | None = None
    news: list[dict[str, Any]] = field(default_factory=list)
    btc_change_24h_pct: float = 0.0
    btc_corr_returns: float = 0.0
    fetched_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class Signal:
    symbol: str
    base: str
    quote: str
    direction: str  # LONG / SHORT
    timeframe: str
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    rr1: float
    rr2: float
    rr3: float
    confidence: float
    rationale: str
    risks: str
    score: float = 0.0
    price: float = 0.0
    atr_pct: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    market: MarketData | None = None
    generated_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return f"{self.symbol} {self.direction}"

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "timeframe": self.timeframe,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "stop_loss": self.stop_loss,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "rr1": self.rr1,
            "rr2": self.rr2,
            "rr3": self.rr3,
            "confidence": self.confidence,
            "score": self.score,
            "rationale": self.rationale,
            "risks": self.risks,
        }
