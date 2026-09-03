"""
Свечные данные.

Freqtrade-style: DataFrame-friendly, но обёрнуты в dataclass для типизации.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass(slots=True)
class Candle:
    """Одна свеча OHLCV."""

    timestamp: int  # ms epoch — время открытия (как у ccxt)
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(slots=True)
class Candles:
    """
    Набор свечей. Удобное API:

    - `.df` — pandas DataFrame (OHLCV + индикаторы добавляются по колонкам).
    - `.closes`, `.highs`, `.lows`, `.volumes` — numpy-массивы.
    - `.last` — последняя свеча.
    - `.append()` / `.extend()` — добавление.
    """

    exchange: str
    symbol: str
    timeframe: str
    _data: List[Candle] = field(default_factory=list)

    # ── Фабрики ────────────────────────────────────────────────
    @classmethod
    def from_ccxt(cls, ohlcv: list, exchange: str, symbol: str, timeframe: str) -> "Candles":
        candles = [
            Candle(
                timestamp=int(c[0]),
                open=float(c[1]),
                high=float(c[2]),
                low=float(c[3]),
                close=float(c[4]),
                volume=float(c[5]),
            )
            for c in ohlcv
        ]
        return cls(exchange=exchange, symbol=symbol, timeframe=timeframe, _data=candles)

    @classmethod
    def empty(cls, exchange: str, symbol: str, timeframe: str) -> "Candles":
        return cls(exchange=exchange, symbol=symbol, timeframe=timeframe)

    # ── Доступ к данным ───────────────────────────────────────
    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def __getitem__(self, idx: int) -> Candle:
        return self._data[idx]

    def append(self, candle: Candle) -> None:
        self._data.append(candle)

    def extend(self, candles: List[Candle]) -> None:
        self._data.extend(candles)

    @property
    def last(self) -> Optional[Candle]:
        return self._data[-1] if self._data else None

    @property
    def df(self) -> pd.DataFrame:
        """DataFrame со свечами. Колонка `timestamp` — int64 ms epoch."""
        if not self._data:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame([c.to_dict() for c in self._data])
        df["timestamp"] = df["timestamp"].astype("int64")
        return df

    @property
    def closes(self) -> np.ndarray:
        return np.array([c.close for c in self._data], dtype=np.float64)

    @property
    def highs(self) -> np.ndarray:
        return np.array([c.high for c in self._data], dtype=np.float64)

    @property
    def lows(self) -> np.ndarray:
        return np.array([c.low for c in self._data], dtype=np.float64)

    @property
    def volumes(self) -> np.ndarray:
        return np.array([c.volume for c in self._data], dtype=np.float64)

    @property
    def opens(self) -> np.ndarray:
        return np.array([c.open for c in self._data], dtype=np.float64)

    # ── Возраст/полнота ───────────────────────────────────────
    @property
    def last_closed_age_seconds(self) -> Optional[int]:
        """
        Возраст последней ЗАКРЫТОЙ свечи в секундах.

        Урок BRAIN.md п.19(а): ccxt метит свечу временем ОТКРЫТИЯ, поэтому
        для базового ТФ 1h последняя закрытая свеча — это «час назад»,
        и гейт свежести должен это учитывать.
        """
        if not self._data:
            return None
        # Время закрытия = открытие + 1 период.
        tf_seconds = _timeframe_to_seconds(self.timeframe)
        last_open_ms = self._data[-1].timestamp
        last_close_ms = last_open_ms + tf_seconds * 1000
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        return max(0, (now_ms - last_close_ms) // 1000)

    @property
    def is_complete(self) -> bool:
        """Свеча считается полной, если закрытие прошло ≥1.5 периода назад."""
        age = self.last_closed_age_seconds
        if age is None:
            return False
        return age >= int(_timeframe_to_seconds(self.timeframe) * 1.5)

    @property
    def completeness_pct(self) -> float:
        """
        Полнота последней свечи 0..1 (1 = закрыта).
        Нужна для гейтов и честной статистики.
        """
        if not self._data:
            return 0.0
        tf_seconds = _timeframe_to_seconds(self.timeframe)
        last_open_ms = self._data[-1].timestamp
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        elapsed = (now_ms - last_open_ms) / 1000.0
        return min(1.0, max(0.0, elapsed / tf_seconds))


def _timeframe_to_seconds(tf: str) -> int:
    """Конвертирует таймфрейм ('1h', '15m', '1d') в секунды."""
    tf = tf.strip().lower()
    if not tf:
        return 60
    unit = tf[-1]
    try:
        value = int(tf[:-1])
    except ValueError:
        return 60
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 60)
    return max(1, value * mult)
