from __future__ import annotations

from dataclasses import replace

from cryptopilot.config import Settings
from cryptopilot.flow import FlowSnapshot
from cryptopilot.indicators import compute_features
from cryptopilot.models import Side, Ticker
from cryptopilot.smart_money import _direction_score, _pre_move_score, _stage


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


def test_live_flow_strengthens_matching_direction(candle_factory) -> None:
    f15 = compute_features(candle_factory(direction=1))
    f1h = compute_features(candle_factory(interval="60", direction=1))
    market = replace(
        _ticker(f15.close, bullish=True),
        open_interest_change_pct=None,
        taker_buy_ratio=0.50,
        orderbook_imbalance=0.0,
    )
    baseline, _, _ = _direction_score(Side.LONG, f15, f1h, market)
    flow = FlowSnapshot(
        symbol="TESTUSDT",
        created_ms=1_000_000,
        age_ms=500,
        price=f15.close,
        notional_60s=250_000,
        notional_prev_60s=100_000,
        notional_5m=800_000,
        delta_60s_usdt=75_000,
        cvd_5m_usdt=120_000,
        delta_ratio_60s=0.30,
        cvd_ratio_5m=0.15,
        volume_burst_ratio=2.5,
        price_change_60s_pct=0.08,
        oi_change_2m_pct=0.25,
        oi_change_prev_2m_pct=0.05,
        oi_acceleration_pct_per_min=0.10,
        absorption=None,
        trade_count_60s=100,
    )

    boosted, reasons, _ = _direction_score(Side.LONG, f15, f1h, market, flow)

    assert boosted > baseline
    assert any("Live" in reason or "Поток" in reason for reason in reasons)


def test_matching_absorption_is_not_treated_as_flow_conflict(candle_factory) -> None:
    f15 = compute_features(candle_factory(direction=1))
    f1h = compute_features(candle_factory(interval="60", direction=1))
    market = replace(
        _ticker(f15.close, bullish=True),
        open_interest_change_pct=None,
        taker_buy_ratio=0.50,
        orderbook_imbalance=0.0,
    )
    flow = FlowSnapshot(
        symbol="TESTUSDT",
        created_ms=1_000_000,
        age_ms=500,
        price=f15.close,
        notional_60s=250_000,
        notional_prev_60s=100_000,
        notional_5m=800_000,
        delta_60s_usdt=-75_000,
        cvd_5m_usdt=20_000,
        delta_ratio_60s=-0.30,
        cvd_ratio_5m=0.025,
        volume_burst_ratio=2.5,
        price_change_60s_pct=-0.02,
        oi_change_2m_pct=0.20,
        oi_change_prev_2m_pct=0.05,
        oi_acceleration_pct_per_min=0.075,
        absorption="BUY_ABSORPTION",
        trade_count_60s=100,
    )

    baseline, _, _ = _direction_score(Side.LONG, f15, f1h, market)
    score, reasons, warnings = _direction_score(Side.LONG, f15, f1h, market, flow)

    assert score > baseline
    assert any("поглощ" in reason.lower() for reason in reasons)
    assert not any("delta сейчас против" in warning for warning in warnings)


def test_prime_pre_move_rewards_coiled_market_before_flow(candle_factory) -> None:
    raw15 = compute_features(candle_factory(direction=1))
    raw1h = compute_features(candle_factory(interval="60", direction=1))
    raw4h = compute_features(candle_factory(interval="240", direction=1))
    f15 = replace(
        raw15,
        breakout_up=False,
        breakout_down=False,
        range_high20=raw15.close / 0.995,
        range_position20=0.90,
        relative_volume20=1.0,
        keltner_squeeze_ratio=1.0,
        bb_width_regime_ratio=0.80,
        vwap_distance_atr=0.50,
        dmi_spread=8.0,
    )
    f1h = replace(
        raw1h,
        close=max(raw1h.close, raw1h.ema50 * 1.02, raw1h.ema200 * 1.03),
        supertrend_direction=1,
    )
    f4h = replace(
        raw4h,
        close=max(raw4h.close, raw4h.ema50 * 1.02),
        supertrend_direction=1,
    )
    market = replace(
        _ticker(f15.close, bullish=True),
        open_interest_change_pct=1.0,
        funding_rate=0.0001,
    )
    flow = FlowSnapshot(
        symbol="TESTUSDT",
        created_ms=1_000_000,
        age_ms=500,
        price=f15.close,
        notional_60s=100_000,
        notional_prev_60s=95_000,
        notional_5m=500_000,
        delta_60s_usdt=10_000,
        cvd_5m_usdt=30_000,
        delta_ratio_60s=0.10,
        cvd_ratio_5m=0.06,
        volume_burst_ratio=1.05,
        price_change_60s_pct=0.04,
        oi_change_2m_pct=0.10,
        oi_change_prev_2m_pct=0.02,
        oi_acceleration_pct_per_min=0.04,
        absorption=None,
        trade_count_60s=80,
        spread_bps=2.0,
        funding_pct=0.01,
    )
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
    )

    score, reasons, blockers = _pre_move_score(
        Side.LONG, f15, f1h, f4h, market, flow, 0.05, settings
    )

    assert score >= settings.prime_min_score
    assert not blockers
    assert any("ещё не" in reason or "до импульса" in reason for reason in reasons)


def test_prime_pre_move_rejects_market_that_already_accelerated(candle_factory) -> None:
    raw15 = compute_features(candle_factory(direction=1))
    raw1h = compute_features(candle_factory(interval="60", direction=1))
    raw4h = compute_features(candle_factory(interval="240", direction=1))
    f15 = replace(
        raw15,
        breakout_up=False,
        breakout_down=False,
        range_high20=raw15.close / 0.995,
        range_position20=0.90,
        relative_volume20=1.8,
        keltner_squeeze_ratio=1.0,
        bb_width_regime_ratio=0.80,
        vwap_distance_atr=0.50,
    )
    f1h = replace(
        raw1h,
        close=max(raw1h.close, raw1h.ema50 * 1.02, raw1h.ema200 * 1.03),
        supertrend_direction=1,
    )
    f4h = replace(
        raw4h,
        close=max(raw4h.close, raw4h.ema50 * 1.02),
        supertrend_direction=1,
    )
    market = replace(_ticker(f15.close, bullish=True), open_interest_change_pct=1.0)
    flow = FlowSnapshot(
        symbol="TESTUSDT",
        created_ms=1_000_000,
        age_ms=500,
        price=f15.close,
        notional_60s=300_000,
        notional_prev_60s=100_000,
        notional_5m=700_000,
        delta_60s_usdt=180_000,
        cvd_5m_usdt=200_000,
        delta_ratio_60s=0.60,
        cvd_ratio_5m=0.28,
        volume_burst_ratio=3.0,
        price_change_60s_pct=0.35,
        oi_change_2m_pct=0.30,
        oi_change_prev_2m_pct=0.05,
        oi_acceleration_pct_per_min=0.15,
        absorption=None,
        trade_count_60s=150,
        spread_bps=2.0,
        funding_pct=0.01,
    )
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
    )

    _, _, blockers = _pre_move_score(
        Side.LONG,
        f15,
        f1h,
        f4h,
        market,
        flow,
        0.60,
        settings,
    )

    assert any("уже" in blocker.lower() for blocker in blockers)
