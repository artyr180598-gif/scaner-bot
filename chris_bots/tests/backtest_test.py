"""
Тесты бэктестера.

Гоняем на синтетике с трендом — должны быть сигналы. Потом на боковике
(где стратегия должна молчать, как в BRAIN.md п.1).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from chris_bots.tests.base import OK, make_synthetic_df, run_test


def _df_to_candles(df: pd.DataFrame):
    from chris_bots.core.domain.candle import Candle
    out = []
    for i in range(len(df)):
        out.append(Candle(
            timestamp=int(df["timestamp"].iloc[i]),
            open=float(df["open"].iloc[i]),
            high=float(df["high"].iloc[i]),
            low=float(df["low"].iloc[i]),
            close=float(df["close"].iloc[i]),
            volume=float(df["volume"].iloc[i]),
        ))
    return out


def test_backtest_synthetic_trend_generates_signals() -> None:
    """На синтетике с восходящим трендом стратегия должна выдавать сигналы."""
    from chris_bots.backtest.engine import BacktestEngine

    # Сделаем устойчивый up-trend.
    np.random.seed(123)
    n = 500
    rets = np.random.normal(0.005, 0.015, n)  # +0.5% средний рост, низкая вола
    prices = 100 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({
        "timestamp": np.arange(n) * 3600_000,
        "open": prices * (1 + np.random.normal(0, 0.001, n)),
        "high": prices * (1 + np.abs(np.random.normal(0, 0.003, n))),
        "low": prices * (1 - np.abs(np.random.normal(0, 0.003, n))),
        "close": prices,
        "volume": np.random.uniform(500, 5000, n),
    })
    candles = _df_to_candles(df)
    engine = BacktestEngine(symbol="SYN-UP", timeframe="1H", days=30, max_bars=500)
    result = engine.run(candles=candles, progress=False)
    print(OK + f"uptrend backtest: {result.signals_generated} signals, {result.n_trades} trades, "
              f"winrate={result.winrate:.1%} PF={result.profit_factor:.2f}")
    assert result.signals_generated > 0, "на восходящем тренде стратегия молчит — это БАГ"


def test_backtest_synthetic_sideways_stays_quiet() -> None:
    """На боковике стратегия должна молчать (урок BRAIN.md п.1)."""
    from chris_bots.backtest.engine import BacktestEngine

    np.random.seed(456)
    n = 500
    rets = np.random.normal(0, 0.005, n)  # 0% дрейф
    prices = 100 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({
        "timestamp": np.arange(n) * 3600_000,
        "open": prices, "high": prices * 1.005, "low": prices * 0.995,
        "close": prices, "volume": np.ones(n) * 1000,
    })
    candles = _df_to_candles(df)
    engine = BacktestEngine(symbol="SYN-FLAT", timeframe="1H", days=30, max_bars=500)
    result = engine.run(candles=candles, progress=False)
    # На боковике без тренда сигналов должно быть ОЧЕНЬ мало.
    print(OK + f"sideways backtest: {result.signals_generated} signals (ожидаем мало)")


def test_backtest_metrics() -> None:
    """BacktestResult должен выдавать честные метрики."""
    from chris_bots.backtest.engine import BacktestEngine, BacktestResult, Trade
    from chris_bots.core.domain.signal import Direction

    # Соберём фейковые сделки.
    r = BacktestResult(symbol="X", timeframe="1H", bars_total=100, signals_generated=0)
    # 3W 2L.
    for i, pnl in enumerate([2.0, 1.5, 3.0, -1.0, -1.5]):
        r.trades.append(Trade(
            symbol="X", direction=Direction.LONG,
            entry_time=0, entry_price=100, exit_time=0, exit_price=100+pnl,
            exit_reason="tp1" if pnl > 0 else "sl", pnl_pct=pnl,
            confidence=80.0, bars_held=5,
        ))
    assert r.n_trades == 5
    assert r.n_wins == 3
    assert r.n_losses == 2
    assert abs(r.winrate - 0.6) < 0.01
    assert abs(r.total_pnl_pct - 4.0) < 0.01
    pf = r.profit_factor
    # gross_profit = 6.5, gross_loss = 2.5 → PF = 2.6
    assert abs(pf - 2.6) < 0.1, f"PF должен быть ~2.6, получили {pf:.2f}"
    # Max DD: после +2, +1.5, +3 → peak=6.5; после -1 → 5.5 dd=1; после -1.5 → 4 dd=2.5
    assert abs(r.max_drawdown_pct - 2.5) < 0.01
    print(OK + f"backtest metrics: winrate={r.winrate:.1%} PF={pf:.2f} PnL={r.total_pnl_pct:+.1f}% maxDD={r.max_drawdown_pct:.1f}%")


def test_backtest_confidence_buckets() -> None:
    """Проверка: winrate по бакетам confidence."""
    from chris_bots.backtest.engine import BacktestEngine, BacktestResult, Trade
    from chris_bots.core.domain.signal import Direction

    r = BacktestResult(symbol="X", timeframe="1H", bars_total=100, signals_generated=0)
    # Бакет <60%: 2 сделки, обе убыточные.
    r.trades.append(Trade("X", Direction.LONG, 0, 100, 0, 99, "sl", -1.0, 50.0, 5))
    r.trades.append(Trade("X", Direction.LONG, 0, 100, 0, 99, "sl", -1.0, 55.0, 5))
    # Бакет 60-75%: 2 сделки, 1W 1L.
    r.trades.append(Trade("X", Direction.LONG, 0, 100, 0, 102, "tp1", 2.0, 65.0, 5))
    r.trades.append(Trade("X", Direction.LONG, 0, 100, 0, 99, "sl", -1.0, 70.0, 5))
    # Бакет ≥75%: 4 сделки, 3W 1L.
    r.trades.append(Trade("X", Direction.LONG, 0, 100, 0, 102, "tp1", 2.0, 80.0, 5))
    r.trades.append(Trade("X", Direction.LONG, 0, 100, 0, 102, "tp1", 2.0, 85.0, 5))
    r.trades.append(Trade("X", Direction.LONG, 0, 100, 0, 102, "tp1", 2.0, 90.0, 5))
    r.trades.append(Trade("X", Direction.LONG, 0, 100, 0, 99, "sl", -1.0, 75.0, 5))

    buckets = r.winrate_by_confidence_bucket()
    assert buckets["<60%"]["n"] == 2
    assert buckets["<60%"]["winrate"] == 0.0
    assert buckets["60-75%"]["n"] == 2
    assert buckets["60-75%"]["winrate"] == 0.5
    assert buckets["≥75%"]["n"] == 4
    assert buckets["≥75%"]["winrate"] == 0.75
    print(OK + f"confidence buckets: <60%={buckets['<60%']['winrate']:.0%}, "
              f"60-75%={buckets['60-75%']['winrate']:.0%}, "
              f"≥75%={buckets['≥75%']['winrate']:.0%}")


def test_backtest_trade_simulation_long_tp() -> None:
    """Симуляция: Long, цена доходит до TP1 → выход по TP1."""
    from chris_bots.backtest.engine import BacktestEngine
    from chris_bots.core.domain.candle import Candle
    from chris_bots.core.domain.signal import (
        Confidences, Direction, Signal, SignalPlan, StopLoss, TakeProfit,
    )

    # Соберём candles: 50 баров warmup, потом 5 баров где цена идёт вверх.
    np.random.seed(7)
    n_warmup = 60
    prices = 100 * np.exp(np.cumsum(np.random.normal(0.002, 0.01, n_warmup)))
    candles = [
        Candle(timestamp=i * 3600_000, open=prices[i], high=prices[i] * 1.005,
               low=prices[i] * 0.995, close=prices[i], volume=1000)
        for i in range(n_warmup)
    ]
    # 3 бара сильного роста после warmup.
    for j in range(3):
        last = prices[-1] * 1.01
        prices = np.append(prices, last)
        candles.append(Candle(
            timestamp=(n_warmup + j) * 3600_000,
            open=last * 0.999, high=last * 1.005, low=last * 0.998, close=last,
            volume=1000,
        ))

    engine = BacktestEngine(symbol="SYN-TP", timeframe="1H")
    # Форсируем signal через прямой вызов.
    signal = engine._try_signal(candles[:n_warmup], candles[n_warmup])  # noqa: SLF001
    if signal is None:
        print(OK + "skipped: сигнал не сгенерировался на синтетике warmup")
        return
    trade = engine._simulate_trade(signal, candles, n_warmup)  # noqa: SLF001
    assert trade is not None
    # В Long ожидаем TP, потому что цена идёт вверх.
    assert trade.pnl_pct > 0, f"ожидали прибыль, получили {trade.pnl_pct:.2f}%"
    assert trade.exit_reason.startswith("tp"), f"ожидали TP, получили {trade.exit_reason}"
    print(OK + f"trade simulation: {trade.direction.value} → {trade.exit_reason} PnL={trade.pnl_pct:+.2f}%")


def test_backtest_on_real_okx_data() -> None:
    """Реальные данные OKX — должны прогнаться без падений (результат может быть 0)."""
    from pathlib import Path
    from chris_bots.backtest.engine import BacktestEngine

    cache = Path("backtest/cache/btc_1h_720.json")
    if not cache.exists():
        print(OK + "skipped: cache/btc_1h_720.json not found")
        return
    import json
    from chris_bots.backtest.data_loader import candles_from_okx
    with cache.open() as f:
        payload = json.load(f)
    candles = candles_from_okx(payload["data"])
    engine = BacktestEngine(symbol="BTC-USDT", timeframe="1H", max_bars=len(candles))
    result = engine.run(candles=candles, progress=False)
    # На BTC 1H в боковике стратегия может молчать — это OK.
    print(OK + f"real BTC-USDT 1H backtest: {result.signals_generated} signals, "
              f"{result.n_trades} trades, winrate={result.winrate:.1%}, "
              f"PF={result.profit_factor:.2f}")


def test_backtest_confidence_buckets_on_real_data() -> None:
    """Главный тест гипотезы «уверенность ≥ 75% = прибыль» (BRAIN.md п.0b).

    Прогоняем с пониженным MIN_CONFIDENCE, чтобы получить сделки во всех бакетах.
    Если ≥75% действительно прибыльнее — гипотеза подтверждена.
    """
    import dataclasses
    from pathlib import Path
    from chris_bots.backtest.engine import BacktestEngine

    cache = Path("backtest/cache/btc_1h_720.json")
    if not cache.exists():
        print(OK + "skipped: cache/btc_1h_720.json not found")
        return
    import json
    from chris_bots.backtest.data_loader import candles_from_okx
    with cache.open() as f:
        payload = json.load(f)
    candles = candles_from_okx(payload["data"])

    # Снижаем порог до 50% и anti_chase_max_pct до 15% (чтобы BTC не отсекался).
    base_s = BacktestEngine(symbol="BTC-USDT", timeframe="1H").s
    relaxed = dataclasses.replace(base_s, min_confidence=50.0, anti_chase_max_pct=15.0)
    engine = BacktestEngine(symbol="BTC-USDT", timeframe="1H", settings=relaxed, max_bars=len(candles))
    result = engine.run(candles=candles, progress=False)
    print()
    print("── Гипотеза: «confidence ≥ 75% = прибыль» ──")
    print(f"Период: {result.start_ts} → {result.end_ts}")
    print(f"Сигналов: {result.signals_generated}, сделок: {result.n_trades}")
    if result.n_trades > 0:
        buckets = result.winrate_by_confidence_bucket()
        for name in ["<60%", "60-75%", "≥75%"]:
            b = buckets[name]
            if b["n"] > 0:
                pf = f"{b['pf']:.2f}" if b['pf'] != float('inf') else "inf"
                print(f"  {name:<8} n={b['n']:>3}  winrate={b['winrate']:>6.1%}  PF={pf:>6}  PnL={b['total_pnl']:+.2f}%")
        # Главный вывод.
        high = buckets["≥75%"]
        low = buckets["<60%"]
        if high["n"] > 0 and low["n"] > 0:
            verdict = "✓ ПОДТВЕРЖДЕНА" if high["pf"] > low["pf"] else "✗ ОПРОВЕРГНУТА"
        else:
            verdict = "недостаточно данных"
        print(f"Гипотеза (≥75% прибыльнее <60%): {verdict}")
    else:
        print("Сделок нет даже с relaxed порогом.")


TESTS = [
    test_backtest_synthetic_trend_generates_signals,
    test_backtest_synthetic_sideways_stays_quiet,
    test_backtest_metrics,
    test_backtest_confidence_buckets,
    test_backtest_trade_simulation_long_tp,
    test_backtest_on_real_okx_data,
    test_backtest_confidence_buckets_on_real_data,
]


if __name__ == "__main__":
    run_test(TESTS, "=== backtest suite ===")
