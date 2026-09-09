"""Causal RID research hypotheses, not a reconstruction of a private indicator.

Only completed candles may be supplied. Neither detector uses future pivots or
historical order-book proxies. Hypotheses cannot authorize production entries.
"""

from dataclasses import dataclass
from statistics import median

from cryptopilot.models import Candle, Side


@dataclass(frozen=True, slots=True)
class RidHypothesis:
    name: str
    side: Side
    trigger: float
    invalidation: float
    volume_ratio: float


def detect_hypotheses(bars: list[Candle], *, allow_short: bool = False) -> list[RidHypothesis]:
    """Closed range breakout after compression or a shallow trend pause.

    The final candle confirms the breakout; all thresholds and levels are computed
    from earlier bars. Parameters are fixed before evaluating the research sample.
    """
    if len(bars) < 60:
        return []
    previous, latest = bars[:-1], bars[-1]
    baseline = previous[-30:-6]
    box = previous[-6:]
    ranges = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in zip(previous[-21:-1], previous[-20:], strict=True)
    ]
    unit = median(ranges)
    normal_volume = median(b.volume for b in baseline)
    if unit <= 0 or normal_volume <= 0:
        return []
    high, low = max(b.high for b in box), min(b.low for b in box)
    width = high - low
    rvol = latest.volume / normal_volume
    # Avoid buying the final giant candle of the very move we wanted to capture.
    if width > 3 * unit or latest.high - latest.low > 2.5 * unit or rvol < 1.5:
        return []
    if median(b.volume for b in box) > normal_volume:
        return []
    closes = [b.close for b in previous]
    fast, slow = closes[0], closes[0]
    for close in closes[1:]:
        fast += (close - fast) * 2 / 21
        slow += (close - slow) * 2 / 51
    found = []
    sides = (Side.LONG, Side.SHORT) if allow_short else (Side.LONG,)
    for side in sides:
        sign = 1 if side is Side.LONG else -1
        level = high if sign == 1 else low
        breakout = sign * (latest.close - level)
        if not (0 < breakout <= 0.5 * unit and sign * (latest.close - latest.open) > 0):
            continue
        trend = sign * (fast - slow) > 0
        approach = sign * (previous[-7].close - previous[-19].close) / unit
        name = None
        if trend and approach >= 2:
            # A trend pause retains the last pre-pause structural extreme.
            origin = (
                min(b.low for b in previous[-19:-6])
                if sign == 1
                else max(b.high for b in previous[-19:-6])
            )
            intact = low > origin if sign == 1 else high < origin
            if intact:
                name = "TREND_PAUSE"
        elif width <= 2 * unit and abs(approach) <= 1.5:
            name = "BASE_BREAKOUT"
        if name:
            stop = low - 0.15 * unit if sign == 1 else high + 0.15 * unit
            found.append(RidHypothesis(name, side, level, stop, rvol))
    return found
