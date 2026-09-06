import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cryptopilot.config import Settings
from cryptopilot.health import RuntimeHealth
from cryptopilot.smart_money import SmartMoneyScanner
from cryptopilot.telegram import build_router


def test_stalled_scan_cancels_requests_releases_lock_and_can_retry(monkeypatch):
    monkeypatch.setattr("cryptopilot.smart_money.SCAN_TIMEOUT_SECONDS", 0.02)

    async def scenario():
        request_cancelled = asyncio.Event()

        async def stall():
            try:
                await asyncio.Event().wait()
            finally:
                request_cancelled.set()

        exchange = SimpleNamespace(name="BYBIT", tickers=AsyncMock(side_effect=stall))
        scanner = SmartMoneyScanner(exchange, Settings(_env_file=None))
        with pytest.raises(TimeoutError):
            await scanner.scan()
        assert request_cancelled.is_set()
        assert not scanner._lock.locked()
        assert scanner.prime_candidates() == ()
        exchange.tickers = AsyncMock(return_value=[])
        report = await scanner.scan()
        assert report.universe_count == 0
        assert await scanner.scan() is report
        exchange.tickers.assert_awaited_once()

    asyncio.run(scenario())


def test_queue_wait_is_bounded_without_cancelling_lock_owner(monkeypatch):
    monkeypatch.setattr("cryptopilot.smart_money.SCAN_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        exchange = SimpleNamespace(name="BYBIT", tickers=AsyncMock())
        scanner = SmartMoneyScanner(exchange, Settings(_env_file=None))
        await scanner._lock.acquire()
        try:
            with pytest.raises(TimeoutError):
                await scanner.scan()
            assert scanner._lock.locked()
            exchange.tickers.assert_not_called()
        finally:
            scanner._lock.release()

    asyncio.run(scenario())


def test_manual_timeout_is_reported_as_data_failure():
    smart = SimpleNamespace(scan=AsyncMock(side_effect=TimeoutError()))
    router = build_router(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                          Settings(_env_file=None), RuntimeHealth(), smart)
    callback = next(h.callback for h in router.message.handlers
                    if h.callback.__name__ == "unified_search")
    progress = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock(return_value=progress))
    asyncio.run(callback(message, SimpleNamespace(clear=AsyncMock())))
    text = progress.edit_text.call_args.args[0]
    assert "жёсткому дедлайну" in text
