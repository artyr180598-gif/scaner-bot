from dataclasses import replace
from types import SimpleNamespace

import numpy as np

from cryptopilot.models import Candle
from scripts.break_retest_study import events


def setup():
    bars = [Candle(i * 900000, 100, 101, 99, 100, 1) for i in range(225)]
    bars[220] = Candle(220 * 900000, 100, 101.5, 100, 101.4, 1)
    bars[221] = Candle(221 * 900000, 101.4, 101.5, 100.95, 101.2, 1)
    f = SimpleNamespace(atr14=np.ones(225), keltner_squeeze_ratio=np.full(225, 0.8))
    return bars, f


def test_retest_cannot_be_on_breakout_bar():
    bars, f = setup()
    found = events(bars, f)
    assert found[0] == ("break", 220, 1, 99)
    assert found[1] == ("retest", 221, 1, 99)


def test_failed_level_cancels_retest():
    bars, f = setup()
    bars[221] = replace(bars[221], close=100.5)
    assert not any(name == "retest" for name, *_ in events(bars, f))


def test_future_changes_do_not_alter_existing_events():
    bars, f = setup()
    before = events(bars[:222], f)
    bars[224] = replace(bars[224], high=200, close=200)
    assert [event for event in events(bars, f) if event[1] < 222] == before
