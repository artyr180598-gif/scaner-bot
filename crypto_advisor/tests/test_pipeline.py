"""Тесты полного пайплайна на синтетической бирже (офлайн)."""

from __future__ import annotations

import asyncio

from crypto_advisor.core.domain.query import UserRequest


def test_find_matches_returns(engine):
    result = asyncio.run(_find(engine, UserRequest.from_text("сбалансированный"), 20))
    assert result.scanned > 0
    top = result.top
    assert all(not m.rejected_reason for m in top)
    if top:
        assert top[0].match_score >= max(0.0, top[-1].match_score)


def test_analyze_symbol_runs(engine):
    req = UserRequest.from_text("сбалансированный лонг")
    sigs = asyncio.run(_analyze_many(engine, req))
    # Не обязана находить сетап, но метод не должен падать.
    assert isinstance(sigs, list)


def test_full_signal_fields(engine):
    req = UserRequest.from_text("агрессивный лонг")
    result = asyncio.run(_find(engine, req, 20))
    assert len(result.top) > 0
    m = result.top[0]
    sig = asyncio.run(engine.analyze_symbol(m.exchange, m.symbol, req))
    if sig is not None:
        assert sig.direction.value in ("Long", "Short")
        assert 0 <= sig.confidences.signal <= 100
        assert sig.plan.take_profits
        assert sig.plan.stop_loss is not None
        assert sig.reason


async def _find(engine, req, top_n):
    return await engine.find_matches(req, top_n=top_n)


async def _analyze_many(engine, req):
    out = []
    for sym in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        sig = await engine.analyze_symbol("synthetic", sym, req)
        if sig is not None:
            out.append(sig)
    return out
