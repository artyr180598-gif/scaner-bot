"""Final executable-quote gate shared by manual and automatic PRIME delivery."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cryptopilot.models import Side

DELIVERY_TIMEOUT_SECONDS = 5

if TYPE_CHECKING:
    from cryptopilot.config import Settings
    from cryptopilot.exchange import ExchangeClient
    from cryptopilot.smart_money import SmartMoneySetup


async def refresh_prime_entry(
    item: SmartMoneySetup, exchange: ExchangeClient, settings: Settings
) -> SmartMoneySetup:
    """Keep the original zone; never move it to chase the latest price.

    Receipt/request age is checked; the ticker model has no exchange timestamp.
    A failed gate removes the plan so every formatter fails closed.
    """
    blockers = list(item.prime_blockers)
    if not item.prime_ready or item.prime_score < settings.prime_min_score:
        blockers.append("Строгие PRIME-подтверждения не пройдены")
    if item.stage == "ENTRY" or item.bias is Side.NO_TRADE:
        blockers.append("Сценарий не является ранним входом")
    if item.exchange != exchange.name:
        blockers.append("Биржа плана не совпадает с источником котировки")
    quote = None
    started = time.monotonic()
    try:
        async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
            quote = next((q for q in await exchange.tickers() if q.symbol == item.symbol), None)
    except Exception:
        blockers.append("Свежая котировка недоступна")
    if time.monotonic() - started > 5:
        blockers.append("Запрос цены занял больше 5 секунд")
    now = datetime.now(UTC)
    if not 0 <= (now - item.created_at).total_seconds() <= 120:
        blockers.append("Анализ старше 2 минут — нужна повторная проверка")
    plan = item.plan
    if plan is None or plan.expires_at <= now:
        blockers.append("План отсутствует или истёк")
    if quote is None or not all(
        math.isfinite(v) and v > 0 for v in (quote.last, quote.bid, quote.ask)
    ):
        blockers.append("Нет корректной исполнимой цены")
    elif quote.ask < quote.bid or quote.spread_bps > settings.max_spread_bps:
        blockers.append("Спред не прошёл ограничение")
    else:
        entry = quote.ask if item.bias is Side.LONG else quote.bid
        if plan is not None and not plan.entry_low <= entry <= plan.entry_high:
            blockers.append("Свежая цена вне исходной зоны — не догонять")
        if (item.bias is Side.LONG and entry >= item.trigger_price) or (
            item.bias is Side.SHORT and entry <= item.trigger_price
        ):
            blockers.append("Уровень пробоя уже пройден — ранний вход отменён")
    if blockers:
        return replace(
            item, prime_ready=False, plan=None, prime_blockers=tuple(dict.fromkeys(blockers))
        )
    return replace(item, price=quote.last)
