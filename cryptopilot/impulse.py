"""Experimental first-breakout detector; profitability is not validated."""
from dataclasses import dataclass
from statistics import median
import math
from cryptopilot.models import Candle

INTERVAL = 300_000


@dataclass(frozen=True)
class Impulse:
    direction: str
    level: float
    close: float
    volume_ratio: float
    extension_atr: float
    closed_at_ms: int


def detect_impulse(candles: list[Candle], now_ms: int, live_price: float) -> Impulse | None:
    """24-bar range vs preceding 24 bars; all baselines exclude event bar."""
    bars = [c for c in candles if c.open_time_ms + INTERVAL <= now_ms][-49:]
    if len(bars) != 49 or not math.isfinite(live_price) or live_price <= 0:
        return None
    if now_ms - (bars[-1].open_time_ms + INTERVAL) >= INTERVAL:
        return None
    for i, c in enumerate(bars):
        if not all(math.isfinite(v) for v in (c.open,c.high,c.low,c.close,c.volume)):
            return None
        if c.low <= 0 or c.volume < 0 or not c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high:
            return None
        if i and c.open_time_ms - bars[i-1].open_time_ms != INTERVAL:
            return None
    older, base, event = bars[:24], bars[24:48], bars[-1]
    high, low = max(c.high for c in base), min(c.low for c in base)
    old_width = max(c.high for c in older) - min(c.low for c in older)
    if high-low <= 0 or old_width <= 0 or (high-low)/old_width > .8:
        return None
    atr = sum(max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close))
              for p,c in zip(bars[33:47],bars[34:48]))/14
    volume = median(c.volume for c in base)
    if atr <= 0 or volume <= 0 or event.volume/volume < 1.5:
        return None
    if event.high-event.low > 2*atr:
        return None
    if event.close > high and low <= event.open <= high:
        sign, direction, level = 1, "LONG", high
    elif event.close < low and low <= event.open <= high:
        sign, direction, level = -1, "SHORT", low
    else:
        return None
    extension = sign*(event.close-level)/atr
    live_extension = sign*(live_price-level)/atr
    if not 0 < extension <= .5 or not 0 < live_extension <= .5:
        return None
    if sign*(event.close-event.open) < .5*(event.high-event.low):
        return None
    return Impulse(direction, level, event.close, event.volume/volume,
                   live_extension, event.open_time_ms+INTERVAL)
