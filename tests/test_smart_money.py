from __future__ import annotations

from cryptopilot.indicators import compute_features
from cryptopilot.models import Side, Ticker
from cryptopilot.smart_money import _direction_score, _stage


def _ticker(price: float, *, bullish: bool) -> Ticker:
    return Ticker(
        symbol="TESTUSDT",
        last=price,
        bid=price * 0.9999,
        ask=price * 1.0001,
        turnover_24h=250_000_000,
        volume_24h=5_000_000,
        funding_rate=0.0001 if bullish else -0.0001,
        open_interest=100_000_000,
        open_interest_change_pct=4.0,
        taker_buy_ratio=0.66 if bullish else 0.34,
        orderbook_imbalance=0.25 if bullish else -0.25,
        long_short_ratio=1.0,
    )


def test_smart_money_score_prefers_bullish_confluence(candle_factory) -> None:
    f15 = compute_features(candle_factory(direction=1))
    f1h = compute_features(candle_factory(interval="60", direction=1))
    ticker = _ticker(f15.close, bullish=True)

    long_score, long_reasons, _ = _direction_score(Side.LONG, f15, f1h, ticker)
    short_score, _, short_warnings = _direction_score(Side.SHORT, f15, f1h, ticker)

    assert long_score > short_score
    assert long_score >= 60
    assert long_reasons
    assert short_warnings


def test_smart_money_score_prefers_bearish_confluence(candle_factory) -> None:
    f15 = compute_features(candle_factory(direction=-1))
    f1h = compute_features(candle_factory(interval="60", direction=-1))
    ticker = _ticker(f15.close, bullish=False)

    short_score, short_reasons, _ = _direction_score(Side.SHORT, f15, f1h, ticker)
    long_score, _, long_warnings = _direction_score(Side.LONG, f15, f1h, ticker)

    assert short_score > long_score
    assert short_score >= 60
    assert short_reasons
    assert long_warnings


def test_entry_stage_requires_confirmed_breakout(candle_factory) -> None:
    f15 = compute_features(candle_factory(direction=1))
    f1h = compute_features(candle_factory(interval="60", direction=1))
    ticker = _ticker(f15.close, bullish=True)

    stage = _stage(Side.LONG, 90, f15, f1h, ticker)

    if f15.breakout_up and f15.relative_volume20 >= 1.2:
        assert stage == "ENTRY"
    else:
        assert stage in {"ARMED", "WATCH"}
