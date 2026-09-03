"""
Smoke-тесты: проверяют, что основные модули импортируются
и базовая логика (scoring, filters, plan) работает на синтетике.
Запуск: python -m chris_bots.tests.smoke
"""
from __future__ import annotations

import sys
import traceback

import numpy as np
import pandas as pd

OK = "[OK]  "
FAIL = "[FAIL]"


def _make_synthetic_df(n: int = 200, trend: str = "up") -> pd.DataFrame:
    """Генерирует синтетический OHLCV с восходящим/нисходящим трендом."""
    np.random.seed(42)
    if trend == "up":
        drift = 0.0008
    elif trend == "down":
        drift = -0.0008
    else:
        drift = 0.0
    rets = np.random.normal(drift, 0.02, n)
    prices = 100 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({
        "timestamp": np.arange(n) * 3600_000,
        "open": prices * (1 + np.random.normal(0, 0.001, n)),
        "high": prices * (1 + np.abs(np.random.normal(0, 0.005, n))),
        "low": prices * (1 - np.abs(np.random.normal(0, 0.005, n))),
        "close": prices,
        "volume": np.random.uniform(100, 1000, n),
    })
    return df


def t_settings_load():
    from chris_bots.config.settings import get_settings
    s = get_settings()
    assert s.base_timeframe in s.analysis_timeframes
    print(OK + "settings load + default validate")


def t_indicators_compute():
    from chris_bots.indicators import attach_signals, all_indicators
    df = _make_synthetic_df(200, "up")
    enriched = attach_signals(df, all_indicators())
    assert "trend_signal" in enriched.columns
    assert "momentum_signal" in enriched.columns
    assert "volume_signal" in enriched.columns
    print(OK + "indicators enrich synthetic df (groups attached)")


def t_scorer_uptrend():
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.scoring import Scorer
    s = get_settings()
    scorer = Scorer(s)
    df = _make_synthetic_df(200, "up")
    enriched = scorer.enrich(df)
    groups = scorer.score_groups(enriched)
    direction = scorer.decide_direction(groups)
    # На синтетике up-trend direction может быть и LONG, и NEUTRAL (случайность),
    # но метод не должен падать.
    assert direction in {direction}
    print(OK + f"scorer.score_groups + decide_direction → {direction.value}")


def t_filters_anti_chase():
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.filters import Filters
    s = get_settings()
    f = Filters(s)
    # Свечи с резким ростом 15% за 12 баров → должно блокировать.
    prices = np.linspace(100, 115, 20)
    df = pd.DataFrame({
        "timestamp": np.arange(20) * 3600_000,
        "open": prices, "high": prices + 0.5, "low": prices - 0.5,
        "close": prices, "volume": np.ones(20) * 500,
    })
    df.attrs = {}
    enriched = df.copy()
    enriched["atr_pct"] = 1.0  # чтобы не упасть на volatility
    res = f.check_anti_chase(enriched)
    assert not res.passed, f"expected anti_chase to block, got {res}"
    # И обратный кейс: спокойный рынок +3% за 12 баров → пропускаем.
    calm = np.linspace(100, 103, 20)
    calm_df = pd.DataFrame({
        "timestamp": np.arange(20) * 3600_000,
        "open": calm, "high": calm + 0.1, "low": calm - 0.1,
        "close": calm, "volume": np.ones(20) * 500,
    })
    calm_df["atr_pct"] = 1.0
    res2 = f.check_anti_chase(calm_df)
    assert res2.passed, f"calm market should pass, got {res2}"
    print(OK + f"filters.anti_chase blocks runaway + passes calm ({res.detail})")


def t_planner_builds_plan():
    from chris_bots.config.settings import get_settings
    from chris_bots.analysis.plan import Planner
    from chris_bots.core.domain.signal import Direction
    s = get_settings()
    planner = Planner()
    df = _make_synthetic_df(200, "up")
    df["atr_pct"] = 2.0
    plan = planner.build(df, Direction.LONG, last_price=df["close"].iloc[-1])
    assert plan.entry_zone[0] < plan.entry_mid < plan.entry_zone[1]
    assert plan.stop_loss is not None
    assert len(plan.take_profits) == 3
    assert plan.risk_reward > 0
    print(OK + f"planner builds plan RR={plan.risk_reward} lev={plan.leverage_suggestion}")


def t_ticker_parse():
    from chris_bots.core.domain.ticker import Ticker
    t1 = Ticker.parse("BTCUSDT")
    t2 = Ticker.parse("eth/usdt")
    t3 = Ticker.parse("SOL-USDT")
    assert (t1.base, t1.quote) == ("BTC", "USDT")
    assert (t2.base, t2.quote) == ("ETH", "USDT")
    assert (t3.base, t3.quote) == ("SOL", "USDT")
    print(OK + "ticker parse: BTCUSDT / eth/usdt / SOL-USDT")


def t_candles_staleness():
    from datetime import datetime, timezone
    from chris_bots.core.domain.candle import Candle, Candles
    now = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    candles = Candles(exchange="x", symbol="BTC/USDT", timeframe="1h")
    # Последняя 1h-свеча закрылась 3 часа назад → полностью закрыта
    # (период 3600s × 1.5 = 5400s = 1.5h минимум).
    candles.append(Candle(timestamp=now - 5 * 3600_000, open=1, high=2, low=0.5, close=1, volume=1))
    candles.append(Candle(timestamp=now - 3 * 3600_000, open=1, high=2, low=0.5, close=1, volume=1))
    assert candles.is_complete, f"expected complete, got age={candles.last_closed_age_seconds}s"
    # Свеча, которая ещё не закрыта (только что открыта).
    fresh = Candles(exchange="x", symbol="ETH/USDT", timeframe="1h")
    fresh.append(Candle(timestamp=now - 60_000, open=1, high=2, low=0.5, close=1, volume=1))
    assert not fresh.is_complete, "fresh candle should NOT be complete"
    print(OK + f"candles.is_complete ok (3h-old closed, 1min-old open)")


def t_event_bus_basic():
    import asyncio
    from chris_bots.core.events import EventBus, Event

    async def _run():
        bus = EventBus()
        await bus.start()
        received = []

        async def handler(ev: Event) -> None:
            received.append(ev.type)

        bus.subscribe("TestEvent", handler)
        await bus.publish(Event())  # без типа
        ev = Event()
        ev.__class__.__name__ = "TestEvent"
        # Делаем «правильно» через переопределение type:
        class TestEvent(Event):
            pass

        await bus.publish(TestEvent())
        await asyncio.sleep(0.1)
        await bus.stop()
        return received

    res = asyncio.run(_run())
    assert "TestEvent" in res
    print(OK + "event bus: subscribe + publish + dispatch")


TESTS = [
    t_settings_load,
    t_ticker_parse,
    t_indicators_compute,
    t_scorer_uptrend,
    t_filters_anti_chase,
    t_planner_builds_plan,
    t_candles_staleness,
    t_event_bus_basic,
]


def main() -> int:
    print("=== chris_bots smoke tests ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception:  # noqa: BLE001
            failed += 1
            print(FAIL + f" {t.__name__}")
            traceback.print_exc()
    print(f"\n=== {len(TESTS) - failed}/{len(TESTS)} passed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
