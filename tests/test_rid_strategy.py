from __future__ import annotations

import time
from dataclasses import replace

import pytest

from cryptopilot.config import Settings
from cryptopilot.exchange import INTERVAL_MS
from cryptopilot.models import Candle, Side, Ticker
from cryptopilot.rid_strategy import analyze_rid_pattern, is_rid_auto_candidate


def _bars(interval: str, direction: int = 1, count: int = 260) -> list[Candle]:
    duration = INTERVAL_MS[interval]
    first = int(time.time() * 1000) - (count + 2) * duration
    rows: list[Candle] = []
    for index in range(count):
        close = 90 + direction * index * 0.04
        rows.append(
            Candle(
                open_time_ms=first + index * duration,
                open=close - direction * 0.02,
                high=close + 0.16,
                low=close - 0.16,
                close=close,
                volume=1000,
                turnover=close * 1000,
            )
        )
    return rows


def _rid_5m(side: Side) -> list[Candle]:
    rows = _bars("5", 1 if side is Side.LONG else -1)
    anchor = rows[-15].close
    sign = 1 if side is Side.LONG else -1
    # A four-candle displacement, then a quieter pullback/compression and finally
    # a close-confirmed reactivation.  Only the final candle is the trigger.
    closes = [
        anchor,
        anchor + sign * 1.5,
        anchor + sign * 3.4,
        anchor + sign * 5.4,
        anchor + sign * 4.8,
        anchor + sign * 3.6,
        anchor + sign * 3.0,
        anchor + sign * 3.1,
        anchor + sign * 3.2,
        anchor + sign * 3.15,
        anchor + sign * 3.1,
        anchor + sign * 4.1,
    ]
    volumes = [1100, 2600, 3400, 4200, 1000, 780, 620, 500, 480, 450, 430, 1250]
    start = len(rows) - len(closes)
    for offset, (close, volume) in enumerate(zip(closes, volumes, strict=True)):
        index = start + offset
        open_price = close - sign * (0.45 if offset in {1, 2, 3, 11} else 0.08)
        rows[index] = replace(
            rows[index],
            open=open_price,
            high=max(open_price, close) + 0.12,
            low=min(open_price, close) - 0.12,
            close=close,
            volume=volume,
            turnover=close * volume,
        )
    return rows


def _ticker(price: float) -> Ticker:
    return Ticker(
        symbol="TESTUSDT",
        last=price,
        bid=price * 0.9999,
        ask=price * 1.0001,
        turnover_24h=100_000_000,
        volume_24h=1_000_000,
        open_interest_change_pct=1.2,
        taker_buy_ratio=0.56,
        orderbook_imbalance=0.08,
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_rid_pattern_requires_impulse_pullback_and_closed_reactivation(side: Side) -> None:
    bars5 = _rid_5m(side)
    direction = 1 if side is Side.LONG else -1
    price = bars5[-1].close
    ticker = _ticker(price)
    if side is Side.SHORT:
        ticker = replace(ticker, taker_buy_ratio=0.44, orderbook_imbalance=-0.08)
    signal = analyze_rid_pattern(
        "TESTUSDT",
        "BYBIT",
        ticker,
        {"5": bars5, "15": _bars("15", direction), "60": _bars("60", direction)},
        Settings(
            _env_file=None,
            rid_manual_min_score=60,
            rid_max_impulse_atr=8,
            rid_short_enabled=True,
        ),
    )

    assert signal.side is side
    assert signal.actionable
    assert signal.strategy_version.startswith("rid-")
    assert signal.fingerprint.startswith("RID:")
    assert signal.market_context["rid_impulse_atr"] >= 2.5
    assert signal.plan is not None and signal.plan.risk_reward_2 >= 1.8
    assert signal.plan.scale_allocations_pct == (50, 30, 20)


def test_rid_rejects_stale_quote_instead_of_chasing() -> None:
    bars5 = _rid_5m(Side.LONG)
    stale = _ticker(bars5[-1].close * 1.08)
    signal = analyze_rid_pattern(
        "TESTUSDT",
        "BYBIT",
        stale,
        {"5": bars5, "15": _bars("15"), "60": _bars("60")},
        Settings(_env_file=None, rid_manual_min_score=60, rid_max_impulse_atr=8),
    )

    assert signal.side is Side.LONG
    assert not signal.actionable
    assert any("несвеж" in blocker for blocker in signal.blockers)


def test_rid_does_not_fire_without_reactivation() -> None:
    bars5 = _rid_5m(Side.LONG)
    last = bars5[-1]
    bars5[-1] = replace(last, open=last.close + 0.5, close=last.close - 0.6)
    signal = analyze_rid_pattern(
        "TESTUSDT",
        "BYBIT",
        _ticker(bars5[-1].close),
        {"5": bars5, "15": _bars("15"), "60": _bars("60")},
        Settings(_env_file=None, rid_manual_min_score=60, rid_max_impulse_atr=8),
    )

    assert signal.side is Side.NO_TRADE
    assert signal.plan is None


def test_rid_auto_waits_for_forward_calibration_and_live_confirmation() -> None:
    bars5 = _rid_5m(Side.LONG)
    settings = Settings(
        _env_file=None,
        rid_manual_min_score=60,
        rid_max_impulse_atr=8,
    )
    signal = analyze_rid_pattern(
        "TESTUSDT",
        "BYBIT",
        _ticker(bars5[-1].close),
        {"5": bars5, "15": _bars("15"), "60": _bars("60")},
        settings,
    )
    assert not is_rid_auto_candidate(signal, settings)

    signal.calibration_samples = settings.rid_auto_min_samples
    signal.estimated_success_pct = settings.rid_auto_min_win_rate + 5
    signal.recent_expectancy_r = settings.rid_auto_min_expectancy_r + 0.1
    assert is_rid_auto_candidate(signal, settings)

    signal.market_context["rid_flow_confirmations"] = 0
    assert not is_rid_auto_candidate(signal, settings)
