import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cryptopilot.config import Settings
from cryptopilot.health import RuntimeHealth
from cryptopilot.models import Side, Ticker
from cryptopilot.prime_delivery import refresh_prime_entry
from cryptopilot.smart_money import SmartMoneyScanner
from cryptopilot.telegram import (
    PRIME,
    SMART_MONEY,
    UNIFIED,
    build_router,
    format_prime_setup,
    main_keyboard,
)


@dataclass
class Candidate:
    symbol: str = "LINKUSDT"
    exchange: str = "BYBIT"
    bias: Side = Side.LONG
    prime_ready: bool = True
    prime_score: int = 95
    stage: str = "ARMED"
    trigger_price: float = 102
    price: float = 100
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    prime_blockers: tuple = ()
    plan: object = field(
        default_factory=lambda: SimpleNamespace(
            entry_low=99, entry_high=101, expires_at=datetime.now(UTC) + timedelta(minutes=10)
        )
    )


def quote(**kwargs):
    return Ticker(
        **dict(symbol="LINKUSDT", last=100, bid=99.99, ask=100.01, turnover_24h=1e8, volume_24h=1e6)
        | kwargs
    )


def check(item, ticker=None, failure=False):
    exchange = SimpleNamespace(
        name="BYBIT",
        tickers=AsyncMock(
            return_value=[ticker or quote()], side_effect=RuntimeError() if failure else None
        ),
    )
    return asyncio.run(refresh_prime_entry(item, exchange, Settings(_env_file=None)))


@pytest.mark.parametrize("side,trigger", [(Side.LONG, 102), (Side.SHORT, 98)])
def test_fresh_executable_zone_is_accepted_without_moving_plan(side, trigger):
    item = Candidate(bias=side, trigger_price=trigger)
    checked = check(item)
    assert checked.prime_ready
    assert checked.plan is item.plan


@pytest.mark.parametrize(
    "ticker",
    [quote(ask=102), quote(bid=101, ask=100), quote(last=float("nan")), quote(bid=99, ask=101)],
)
def test_bad_or_outside_quote_never_produces_plan(ticker):
    checked = check(Candidate(), ticker)
    assert not checked.prime_ready and checked.plan is None
    assert "НЕ ВХОДИТЬ" in format_prime_setup(checked)
    assert "ПОКУПАТЬ" not in format_prime_setup(checked)


@pytest.mark.parametrize(
    "changes",
    [
        {"created_at": datetime.now(UTC) - timedelta(minutes=3)},
        {"prime_ready": False},
        {"prime_score": 60},
        {"trigger_price": 100},
        {"stage": "ENTRY"},
        {"prime_blockers": ("Нет spot",)},
        {"plan": None},
    ],
)
def test_missing_confirmation_or_stale_setup_fails_closed(changes):
    assert check(replace(Candidate(), **changes)).plan is None


def test_network_failure_does_not_reuse_old_price():
    assert check(Candidate(), failure=True).plan is None


def test_stalled_quote_is_cancelled_and_plan_removed(monkeypatch):
    monkeypatch.setattr("cryptopilot.prime_delivery.DELIVERY_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        cancelled = asyncio.Event()

        async def stall():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        exchange = SimpleNamespace(name="BYBIT", tickers=stall)
        checked = await refresh_prime_entry(Candidate(), exchange, Settings(_env_file=None))
        assert not checked.prime_ready and checked.plan is None
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_early_shortlist_cannot_be_displaced_by_eight_active_movers():
    def row(symbol, breakout=False):
        return (
            30,
            SimpleNamespace(symbol=symbol),
            SimpleNamespace(breakout_up=breakout, breakout_down=False),
        )

    early = [row(f"E{i}") for i in range(12)]
    active = [row(f"A{i}", True) for i in range(8)]
    selected = SmartMoneyScanner._deep_candidates(early, active, 16)
    assert selected[:12] == early
    assert len(selected) == 16
    assert len({r[1].symbol for r in selected}) == 16


def test_menu_has_one_main_search():
    buttons = [b.text for row in main_keyboard().keyboard for b in row]
    assert UNIFIED in buttons
    assert PRIME not in buttons and SMART_MONEY not in buttons


def test_no_early_candidates_means_empty_shortlist():
    row = (
        27,
        SimpleNamespace(symbol="NO"),
        SimpleNamespace(breakout_up=False, breakout_down=False),
    )
    assert SmartMoneyScanner._deep_candidates([row], [], 16) == []


def test_manual_search_does_not_fall_back_to_weaker_scanner():
    scanner = SimpleNamespace(scan_market=AsyncMock())
    report = SimpleNamespace(
        finished_at=datetime.now(UTC),
        universe_count=100,
        analyzed_count=0,
        errors=(),
        setups=(),
    )
    smart = SimpleNamespace(
        scan=AsyncMock(return_value=report),
        prime_candidates=lambda: (),
        flow_watchlist=lambda: {},
    )
    router = build_router(
        scanner,
        SimpleNamespace(),
        SimpleNamespace(),
        Settings(_env_file=None),
        RuntimeHealth(),
        smart,
    )
    callbacks = [
        h.callback for h in router.message.handlers if h.callback.__name__ == "unified_search"
    ]
    # All five command aliases and the keyboard filter share exactly one callback.
    assert len(callbacks) == 6 and len(set(callbacks)) == 1
    progress = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock(return_value=progress))
    asyncio.run(callbacks[0](message, SimpleNamespace(clear=AsyncMock())))
    assert "NO TRADE" in message.answer.call_args_list[-1].args[0]
    scanner.scan_market.assert_not_called()
