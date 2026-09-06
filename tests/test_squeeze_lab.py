import asyncio
from dataclasses import replace

from cryptopilot.models import Candle, Ticker
from cryptopilot.squeeze_lab import VERSION, SqueezeLab, advance, fill_plan


def position(entry_ms=60000):
    return dict(
        version=VERSION,
        side=1,
        entry=100.0,
        stop=97.0,
        target=106.0,
        entry_ms=entry_ms,
        expires_ms=entry_ms + 72 * 3600000,
        cursor_ms=60000,
        status="OPEN",
    )


def test_first_partial_minute_cannot_manufacture_a_win():
    result = advance(position(61000), [Candle(60000, 100, 107, 99, 106, 1)], 120000)
    assert result["status"] == "CENSORED_ENTRY_MINUTE"
    assert "net_r" not in result


def test_stop_first_and_gap_not_clipped():
    result = advance(position(), [Candle(60000, 90, 107, 89, 100, 1)], 120000)
    assert result["outcome"] == "SL"
    assert result["net_r"] < -3
    assert result["stress_r"] < result["net_r"]


def test_incomplete_bar_not_used_and_gap_censored():
    bar = Candle(60000, 100, 107, 99, 106, 1)
    assert advance(position(), [bar], 119999)["status"] == "OPEN"
    assert (
        advance(position(), [replace(bar, open_time_ms=120000)], 180000)["status"] == "CENSORED_GAP"
    )


def test_closed_minute_is_not_replayed():
    bar = Candle(60000, 100, 101, 99, 100, 1)
    p = advance(position(), [bar], 120000)
    assert p["cursor_ms"] == 120000
    assert advance(p, [bar], 120000) == p


def test_short_stop_gap():
    p = dict(position(), side=-1, stop=103.0, target=94.0)
    result = advance(p, [Candle(60000, 110, 111, 93, 100, 1)], 120000)
    assert result["net_r"] < -3


def test_quote_not_historical_close_and_expired_entry_rejected():
    candidate = dict(side=1, close=100.0, atr=2.0, swing=97.0, signal_ms=60000)
    ticker = Ticker("BTCUSDT", 100.0, 99.99, 100.01, 1e9, 1e6)
    plan = fill_plan(candidate, ticker, 61000)
    assert plan is not None and plan["entry"] == ticker.ask
    assert fill_plan(candidate, ticker, 151000) is None
    assert fill_plan(candidate, replace(ticker, ask=105.0), 61000) is None


def test_persistence_and_dedup_separate_from_live_signals(tmp_path):
    asyncio.run(check_persistence(tmp_path))


async def check_persistence(tmp_path):
    from types import SimpleNamespace

    lab = SqueezeLab(None, SimpleNamespace(path=tmp_path / "lab.db"), None)
    await lab.initialize()
    await lab.save("key", "BTCUSDT", position())
    await lab.save("key", "BTCUSDT", dict(position(), status="CENSORED_GAP"))
    restarted = SqueezeLab(None, SimpleNamespace(path=lab.path), None)
    assert len(await restarted.rows()) == 1
    assert "пропуск минутных данных: 1" in await restarted.report()


def test_cycle_records_fresh_entry_once_without_alerts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import cryptopilot.squeeze_lab as module

    class Exchange:
        name = "BYBIT"

        async def candles(self, symbol, interval, limit):
            return []

        async def tickers(self):
            return [Ticker("BTCUSDT", 100.0, 99.99, 100.01, 1e9, 1e6)]

    monkeypatch.setattr(module, "SYMBOLS", ("BTCUSDT",))
    monkeypatch.setattr(module.time, "time", lambda: 900.01)
    monkeypatch.setattr(
        module,
        "detect",
        lambda *args: dict(side=1, close=100.0, atr=2.0, swing=97.0, signal_ms=900000),
    )

    async def scenario():
        store = SimpleNamespace(path=tmp_path / "cycle.db")
        settings = SimpleNamespace(max_spread_bps=10, min_volume_usdt=1e6)
        lab = SqueezeLab(Exchange(), store, settings)
        await lab.initialize()
        await lab.cycle()
        assert len(await lab.rows()) == 1
        restarted = SqueezeLab(Exchange(), store, settings)
        await restarted.cycle()
        assert len(await restarted.rows()) == 1

    asyncio.run(scenario())
