from dataclasses import replace
from math import sin

from cryptopilot.models import Candle
from scripts.rid_overlay_study import confirmed_pivot, events


def test_pivot_is_only_available_after_two_right_bars():
    bars = [Candle(i, 1, h, 0, 1, 1) for i, h in enumerate([2, 3, 5, 3, 2])]
    assert confirmed_pivot(bars, 3, 1) is None
    assert confirmed_pivot(bars, 4, 1) == 5
    bars[4] = replace(bars[4], high=6)
    assert confirmed_pivot(bars, 4, 1) is None


def test_future_candles_do_not_change_earlier_events():
    bars = [
        Candle(i, 100, 101 + 5 * sin(i / 10), 99 + 5 * sin(i / 10), 100 + 5 * sin(i / 10), 1)
        for i in range(320)
    ]
    prefix_events = events(bars[:250])
    assert prefix_events
    changed = bars[:250] + [replace(b, close=b.close * 2, high=b.high * 2) for b in bars[250:]]
    assert [e for e in events(changed) if e[1] < 250] == prefix_events
