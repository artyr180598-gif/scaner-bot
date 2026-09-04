from __future__ import annotations

from cryptopilot.config import Settings
from cryptopilot.engine import SignalEngine
from cryptopilot.models import Side, Ticker


def settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="test",
        telegram_chat_id="1",
        min_volume_usdt=1_000_000,
        timeframes="15,60,240",
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
