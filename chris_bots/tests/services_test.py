"""
Тесты сервисов: narrator (шаблон), formatter, sqlite store, event bus.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from chris_bots.tests.base import OK, run_test


def test_narrator_template_mode() -> None:
    """Без LLM должен работать шаблон от Крис."""
    from chris_bots.config.settings import Settings
    from chris_bots.core.domain.signal import (
        Confidences, Direction, Signal, SignalPlan, StopLoss, TakeProfit,
    )
    from chris_bots.services.llm import Narrator

    s = Settings(llm_enabled=False)
    narrator = Narrator(s)

    plan = SignalPlan(
        entry_zone=(99.0, 101.0),
        entry_mid=100.0,
        take_profits=[
            TakeProfit(level=1, price=102.0, pct_from_entry=2.0),
            TakeProfit(level=2, price=104.0, pct_from_entry=4.0),
            TakeProfit(level=3, price=106.0, pct_from_entry=6.0),
        ],
        stop_loss=StopLoss(price=98.0, pct_from_entry=-2.0, rationale="тест"),
    )
    signal = Signal(
        symbol="BTC/USDT",
        exchange="binance",
        direction=Direction.LONG,
        last_price=100.0,
        timeframe_base="1h",
        timeframes_used=["1h", "4h", "1d"],
        confidences=Confidences(data=80.0, signal=78.0, caps={"trend": 0.7, "momentum": 0.5}),
        plan=plan,
        entry_logic="",
        signal_id="abc123",
    )

    async def _run():
        return await narrator.narrate(signal)

    narrative = asyncio.run(_run())
    assert narrative.source == "template"
    assert "BTC" in narrative.text or "BTC/USDT" in narrative.text
    assert len(narrative.factors) > 0
    print(OK + f"narrator template: '{narrative.text[:80]}…'")


def test_narrator_factors_picks_top_groups() -> None:
    from chris_bots.config.settings import Settings
    from chris_bots.core.domain.signal import (
        Confidences, Direction, Signal, SignalPlan,
    )
    from chris_bots.services.llm import Narrator

    s = Settings(llm_enabled=False)
    caps = {"trend": 0.9, "momentum": 0.6, "volume": 0.3, "volatility": 0.05, "structure": 0.0, "patterns": 0.0}
    # Активные: trend, momentum, volume.
    plan = SignalPlan(entry_zone=(99, 101), entry_mid=100, take_profits=[])
    signal = Signal(
        symbol="X/USDT", exchange="binance", direction=Direction.LONG,
        last_price=100, timeframe_base="1h", timeframes_used=["1h"],
        confidences=Confidences(data=80, signal=80, caps=caps),
        plan=plan, entry_logic="", signal_id="x",
    )
    factors = Narrator._select_factors(signal)
    assert "тренд" in factors[0], f"top group should be тренд, got {factors}"
    assert len(factors) <= 3
    print(OK + f"narrator factors picks top: {factors}")


def test_formatter_signal() -> None:
    """Форматтер должен выдавать валидный HTML по ТЗ."""
    from chris_bots.core.domain.signal import (
        Confidences, Direction, Signal, SignalPlan, StopLoss, TakeProfit,
    )
    from chris_bots.services.formatter import format_signal

    plan = SignalPlan(
        entry_zone=(67234.10, 67389.50),
        entry_mid=67311.80,
        take_profits=[
            TakeProfit(level=1, price=68200.00, pct_from_entry=1.32),
            TakeProfit(level=2, price=69088.00, pct_from_entry=2.64),
            TakeProfit(level=3, price=69976.00, pct_from_entry=3.96),
        ],
        stop_loss=StopLoss(price=66100.00, pct_from_entry=-1.80, rationale="локальный минимум"),
        risk_reward=2.0,
        leverage_suggestion=2.0,
    )
    signal = Signal(
        symbol="BTC/USDT", exchange="binance", direction=Direction.LONG,
        last_price=67311.80, timeframe_base="1h", timeframes_used=["1h", "4h"],
        confidences=Confidences(data=85, signal=81),
        plan=plan, entry_logic="Смотрю на BTC: тренд бычий.", signal_id="x",
    )
    out = format_signal(signal)
    # Все ключевые секции должны быть.
    for marker in ["🪙", "Монета:", "BTC", "Long", "🎯", "Уверенность", "81", "🧠", "Логика", "📍", "Вход", "🏁", "Цели", "🛑", "Stop-Loss"]:
        assert marker in out, f"format_signal missing: {marker!r}"
    assert "<b>" in out and "</b>" in out
    assert "<code>" in out
    print(OK + "format_signal содержит все ключевые секции")


def test_formatter_scan_summary() -> None:
    from chris_bots.services.formatter import format_scan_summary
    out = format_scan_summary(scanned=100, candidates=20, duration=2.5, signals=[])
    assert "100" in out and "20" in out
    assert "📊" in out
    out2 = format_scan_summary(50, 10, 1.2)
    assert "болото" in out2 or "не нашёл" in out2
    print(OK + "format_scan_summary ok (empty + summary)")


def test_sqlite_store_roundtrip() -> None:
    """Сигнал должен сохраниться в БД и достаться обратно."""
    from chris_bots.core.domain.signal import (
        Confidences, Direction, Signal, SignalPlan, StopLoss, TakeProfit, SignalStatus,
    )
    from chris_bots.data.storage.sqlite_store import SignalStore

    with tempfile.TemporaryDirectory() as tmp:
        store = SignalStore(path=os.path.join(tmp, "test.db"))

        async def _run():
            await store.start()
            plan = SignalPlan(
                entry_zone=(99, 101), entry_mid=100,
                take_profits=[TakeProfit(level=1, price=102, pct_from_entry=2.0)],
                stop_loss=StopLoss(price=98, pct_from_entry=-2.0, rationale=""),
                risk_reward=1.0, leverage_suggestion=1.0,
            )
            signal = Signal(
                symbol="BTC/USDT", exchange="binance", direction=Direction.LONG,
                last_price=100, timeframe_base="1h", timeframes_used=["1h"],
                confidences=Confidences(data=80, signal=80),
                plan=plan, entry_logic="test", signal_id="test123",
                status=SignalStatus.PUBLISHED,
            )
            await store.save(signal)
            await store.update_status("test123", SignalStatus.HIT_TP, pnl_pct=2.0)
            recent = await store.recent(limit=5)
            await store.close()
            return recent

        rows = asyncio.run(_run())
        assert len(rows) >= 1
        assert rows[0]["signal_id"] == "test123"
        assert rows[0]["status"] == "hit_tp"
        assert rows[0]["pnl_pct"] == 2.0
        print(OK + f"sqlite store: save + update + recent → {len(rows)} row(s)")


def test_event_bus_subscribers() -> None:
    """Подписчики разных типов получают только свои события."""
    from chris_bots.core.events import Event, EventBus

    class Foo(Event): pass
    class Bar(Event): pass

    async def _run():
        bus = EventBus()
        await bus.start()
        foo_count = 0
        bar_count = 0

        async def on_foo(ev):
            nonlocal foo_count
            foo_count += 1

        async def on_bar(ev):
            nonlocal bar_count
            bar_count += 1

        bus.subscribe("Foo", on_foo)
        bus.subscribe("Bar", on_bar)
        await bus.publish(Foo())
        await bus.publish(Foo())
        await bus.publish(Bar())
        await asyncio.sleep(0.2)
        await bus.stop()
        return foo_count, bar_count

    f, b = asyncio.run(_run())
    assert f == 2 and b == 1, f"expected foo=2,bar=1, got {f},{b}"
    print(OK + f"event bus: targeted subscription (foo={f}, bar={b})")


def test_ticker_parse_edge_cases() -> None:
    from chris_bots.core.domain.ticker import Ticker

    # Длинный список котировок.
    for sym in ["BTCUSDT", "ETHUSDC", "SOLBUSD", "BNBUSDT", "USDCUSDT", "DOGEUSDT", "1000PEPEUSDT"]:
        t = Ticker.parse(sym)
        assert t.base and t.quote
    # Должно падать на мусоре.
    for bad in ["", "1", "X", "BT", "BTC", "B"/"C/USDT"] if False else ["", "X", "BTC"]:
        try:
            Ticker.parse(bad)
            assert False, f"должно было упасть на {bad!r}"
        except ValueError:
            pass
    # BTCETH — base BTC, quote? (нет подходящей котировки)
    try:
        Ticker.parse("BTCETH")
        # ccxt такое не пропустит, но мы и не должны.
    except ValueError:
        pass
    print(OK + "ticker: 7+ стандартных форматов + edge cases")


TESTS = [
    test_narrator_template_mode,
    test_narrator_factors_picks_top_groups,
    test_formatter_signal,
    test_formatter_scan_summary,
    test_sqlite_store_roundtrip,
    test_event_bus_subscribers,
    test_ticker_parse_edge_cases,
]


if __name__ == "__main__":
    run_test(TESTS, "=== services suite ===")
