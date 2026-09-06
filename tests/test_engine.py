from __future__ import annotations

from cryptopilot.config import Settings
from cryptopilot.engine import SignalEngine
from cryptopilot.models import Side, Ticker


def test_engine_vetoes_plan_that_only_passes_before_costs(candle_factory, monkeypatch):
    from dataclasses import replace

    config = settings()
    series = {tf: candle_factory(interval=tf, direction=1) for tf in config.timeframe_list}
    actual = series["15"][-1].close
    market = replace(ticker(), last=actual, bid=actual * 0.99995, ask=actual * 1.00005)
    engine = SignalEngine(config)
    original = engine._build_plan

    def tight_plan(*args, **kwargs):
        plan = original(*args, **kwargs)
        return replace(
            plan,
            entry_low=actual,
            entry_high=actual,
            stop_loss=actual * 0.99,
            take_profit_2=actual * 1.02,
        )

    monkeypatch.setattr(engine, "_build_plan", tight_plan)
    result = engine.analyze("TESTUSDT", "BYBIT", market, series, series["240"])
    assert result.side is Side.NO_TRADE
    assert any("после расчётных издержек" in r for r in result.blockers)
    config.paper_one_way_cost_bps = 0
    assert engine.analyze("TESTUSDT", "BYBIT", market, series, series["240"]).side is Side.LONG


def settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
        min_volume_usdt=1_000_000,
        timeframes="15,60,240",
        main_scan_premove_only=False,
    )


def ticker(volume: float = 200_000_000) -> Ticker:
    return Ticker(
        symbol="TESTUSDT",
        last=147.5,
        bid=147.49,
        ask=147.51,
        turnover_24h=volume,
        volume_24h=2_000_000,
        funding_rate=0.0001,
        open_interest=50_000_000,
    )


def test_engine_builds_explainable_long_plan(candle_factory) -> None:
    config = settings()
    series = {tf: candle_factory(interval=tf, direction=1) for tf in config.timeframe_list}
    actual_price = series["15"][-1].close
    market = ticker()
    market = Ticker(
        symbol=market.symbol,
        last=actual_price,
        bid=actual_price * 0.99995,
        ask=actual_price * 1.00005,
        turnover_24h=market.turnover_24h,
        volume_24h=market.volume_24h,
        funding_rate=market.funding_rate,
        open_interest=market.open_interest,
    )
    signal = SignalEngine(config).analyze("TESTUSDT", "BYBIT", market, series, series["240"])

    assert signal.side is Side.LONG
    assert signal.plan is not None
    assert signal.plan.stop_loss < signal.price < signal.plan.take_profit_1
    assert signal.plan.take_profit_2 > signal.plan.take_profit_1
    assert signal.plan.scale_allocations_pct == (50, 30, 20)
    assert signal.plan.scale_entries[0] > signal.plan.scale_entries[1]
    assert signal.plan.scale_entries[1] > signal.plan.scale_entries[2]
    assert 1 <= signal.plan.recommended_leverage <= signal.plan.max_leverage <= 3
    assert 50 <= signal.confidence <= 89
    assert signal.reasons


def test_engine_rejects_illiquid_market(candle_factory) -> None:
    config = settings()
    series = {tf: candle_factory(interval=tf, direction=1) for tf in config.timeframe_list}
    actual_price = series["15"][-1].close
    market = ticker(volume=100)
    market = Ticker(
        symbol=market.symbol,
        last=actual_price,
        bid=actual_price * 0.99995,
        ask=actual_price * 1.00005,
        turnover_24h=market.turnover_24h,
        volume_24h=market.volume_24h,
        funding_rate=market.funding_rate,
        open_interest=market.open_interest,
    )
    signal = SignalEngine(config).analyze("TESTUSDT", "BYBIT", market, series, series["240"])

    assert signal.side is Side.NO_TRADE
    assert any("ликвидности" in reason for reason in signal.blockers)


def test_premove_gate_rejects_already_expanded_execution(candle_factory) -> None:
    from dataclasses import replace

    from cryptopilot.indicators import compute_features

    config = Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
        min_volume_usdt=1_000_000,
        main_scan_premove_only=True,
        main_scan_min_premove_readiness=50,
    )
    engine = SignalEngine(config)
    execution = compute_features(candle_factory(interval="15", direction=1))
    primary = compute_features(candle_factory(interval="60", direction=1))
    structural = compute_features(candle_factory(interval="240", direction=1))
    execution = replace(
        execution,
        breakout_up=True,
        relative_volume20=1.9,
        range_high20=execution.close * 1.002,
        range_position20=0.95,
        supertrend_direction=1,
    )
    primary = replace(primary, breakout_up=False, breakout_down=False, supertrend_direction=1)
    structural = replace(structural, supertrend_direction=1)
    market = replace(
        ticker(),
        last=execution.close,
        bid=execution.close * 0.9999,
        ask=execution.close * 1.0001,
        open_interest_change_pct=1.0,
        taker_buy_ratio=0.60,
    )

    _, _, _, _, _, blockers = engine._main_premove_score(
        Side.LONG,
        execution,
        primary,
        structural,
        market,
        candle_factory(interval="15", direction=1),
    )

    assert any("пробой" in item.lower() or "rvol" in item.lower() for item in blockers)


def test_quick_opportunity_score_penalizes_price_expansion(candle_factory) -> None:
    from dataclasses import replace

    config = Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
    )
    engine = SignalEngine(config)
    base = candle_factory(interval="15", direction=0)
    expanded = list(base)
    last = expanded[-1]
    expanded[-1] = replace(
        last,
        open=last.open,
        high=last.high * 1.12,
        low=last.low,
        close=last.close * 1.10,
        volume=last.volume * 8,
        turnover=last.turnover * 8,
    )

    assert engine.quick_opportunity_score(expanded) < engine.quick_opportunity_score(base)
