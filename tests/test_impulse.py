from dataclasses import replace
from cryptopilot.impulse import detect_impulse, INTERVAL
from cryptopilot.models import Candle


def series():
    bars = [Candle(i*INTERVAL,100,102,98,100,100) for i in range(24)]
    bars += [Candle(i*INTERVAL,100,101,99,100,100) for i in range(24,48)]
    bars += [Candle(48*INTERVAL,100.8,101.5,100.7,101.4,200)]
    return bars


def test_first_breakout_and_mirrored_short():
    bars = series()
    result = detect_impulse(bars,49*INTERVAL,101.4)
    assert result.direction == "LONG" and result.level == 101
    mirrored = [replace(c,open=200-c.open,high=200-c.low,low=200-c.high,close=200-c.close) for c in bars]
    assert detect_impulse(mirrored,49*INTERVAL,98.6).direction == "SHORT"


def test_open_future_bars_do_not_change_result():
    bars = series()
    expected = detect_impulse(bars,49*INTERVAL,101.4)
    bars.append(Candle(49*INTERVAL,101,200,1,150,999999))
    assert detect_impulse(bars,49*INTERVAL,101.4) == expected


def test_reject_stale_gapped_missing_volume_and_chasing():
    bars = series()
    assert detect_impulse(bars,50*INTERVAL,101.4) is None
    assert detect_impulse(bars[:20]+bars[21:],49*INTERVAL,101.4) is None
    assert detect_impulse(bars,49*INTERVAL,103) is None
    assert detect_impulse(bars,49*INTERVAL,100.9) is None
    bars[-1] = replace(bars[-1],volume=100)
    assert detect_impulse(bars,49*INTERVAL,101.4) is None


def test_reject_huge_candle_and_gap_open():
    bars = series()
    bars[-1] = replace(bars[-1],high=108)
    assert detect_impulse(bars,49*INTERVAL,101.4) is None
    bars = series()
    bars[-1] = replace(bars[-1],open=101.2)
    assert detect_impulse(bars,49*INTERVAL,101.4) is None
