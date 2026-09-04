from __future__ import annotations

import math
import time

import pytest

from cryptopilot.exchange import INTERVAL_MS
from cryptopilot.models import Candle


@pytest.fixture
def candle_factory():
    def build(interval: str = "60", count: int = 300, direction: int = 1) -> list[Candle]:
        duration = INTERVAL_MS[interval]
        first = int(time.time() * 1000) - (count + 1) * duration
        output: list[Candle] = []
        for index in range(count):
            trend = math.exp(direction * index * 0.0013)
            wave = 1 + math.sin(index / 6) * 0.0015
            close = 100 * trend * wave
            open_price = close * (1 - direction * 0.0005)
            output.append(
                Candle(
                    open_time_ms=first + index * duration,
                    open=open_price,
                    high=max(open_price, close) * 1.003,
                    low=min(open_price, close) * 0.997,
                    close=close,
                    volume=1000 + (index % 20) * 10,
                    turnover=close * 1000,
                )
            )
        return output

    return build
