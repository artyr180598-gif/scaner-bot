"""Снимок рынка — что видит сканер в один момент времени."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .candle import Candles


@dataclass(slots=True)
class TickerMeta:
    """Метаданные тикера с биржи."""

    symbol: str
    exchange: str
    last_price: float
    quote_volume_24h: float  # в USD (для анти-мусор фильтра)
    change_pct_24h: float
    # «Гонка» — насколько цена уже ушла за последние N баров (%)
    recent_move_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0

    @property
    def is_liquid(self) -> bool:
        return self.quote_volume_24h >= 5_000_000


@dataclass(slots=True)
class MarketSnapshot:
    """
    Снимок рынка: тикеры + мульти-таймфреймовые свечи.

    Используется сканером → анализом → построением сигнала.
    """

    exchange: str
    tickers: List[TickerMeta] = field(default_factory=list)
    # symbol → timeframe → Candles
    candles: Dict[str, Dict[str, Candles]] = field(default_factory=dict)
    # Служебное: время снимка
    captured_at: float = 0.0

    def get_candles(self, symbol: str, timeframe: str) -> Optional[Candles]:
        return self.candles.get(symbol, {}).get(timeframe)

    def set_candles(self, candles: Candles) -> None:
        self.candles.setdefault(candles.symbol, {})[candles.timeframe] = candles
