from dataclasses import replace

from cryptopilot.models import Candle
from cryptopilot.rid_hypotheses import detect_hypotheses


def sample():
    rows = [Candle(i * 900000, 100, 100.5, 99.5, 100, 1000) for i in range(60)]
    rows[-1] = replace(rows[-1], close=100.7, high=100.8, volume=1800)
    return rows


def test_base_breakout_requires_volume_and_close():
    rows = sample()
    assert detect_hypotheses(rows)[0].name == "BASE_BREAKOUT"
    assert detect_hypotheses(rows)[0].trigger == 100.5
    rows[-1] = replace(rows[-1], volume=900)
    assert not detect_hypotheses(rows)
    rows[-1] = replace(rows[-1], volume=1800, close=100.4)
    assert not detect_hypotheses(rows)


def test_reject_chasing_and_short_history():
    rows = sample()
    assert not detect_hypotheses(rows[-40:])
    rows[-1] = replace(rows[-1], high=104, close=103)
    assert not detect_hypotheses(rows)


def test_trend_pause_is_distinct_from_base():
    rows = sample()
    for i in range(41, 53):
        close = 100 + (i - 41) * 0.3
        rows[i] = replace(rows[i], open=close, close=close, high=close + 0.5, low=close - 0.5)
    for i in range(53, 59):
        rows[i] = replace(rows[i], open=103.3, close=103.3, high=103.8, low=102.8, volume=700)
    rows[-1] = replace(rows[-1], open=103.3, close=104, high=104.1, low=103.2)
    assert detect_hypotheses(rows)[0].name == "TREND_PAUSE"


def test_replay_does_not_fill_at_signal_close_or_ignore_gap_stop():
    from scripts.rid_hypothesis_replay import replay

    rows = sample()
    # Move the trigger one candle forward to satisfy replay warmup.
    rows.insert(0, replace(rows[0], open_time_ms=-900000))
    for i in range(60, 160):
        rows.append(Candle(i * 900000, 100.7, 100.9, 100.6, 100.7, 1000))
    rows[62] = replace(rows[62], open=98, high=99, low=97, close=98)
    records = replay(rows)
    assert len(records) == 1
    assert records[0]["outcome"] == "SL"
    assert records[0]["net_r"] < -1


def test_research_candidate_has_no_executable_plan():
    from datetime import UTC, datetime

    from cryptopilot.config import Settings
    from cryptopilot.models import Side, Signal
    from cryptopilot.rid_strategy import is_rid_auto_candidate
    from cryptopilot.telegram import format_rid_candidate

    signal = Signal(
        "TESTUSDT",
        "BYBIT",
        Side.LONG,
        0,
        0,
        "RID_RESEARCH",
        100,
        datetime.now(UTC),
        market_context={"rid_research_only": 1, "rid_research_invalidation": 99},
    )
    assert not signal.actionable
    assert not is_rid_auto_candidate(signal, Settings(_env_file=None))
    assert "не сигнал на вход" in format_rid_candidate(signal)
