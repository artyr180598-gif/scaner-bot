"""Background price-alert watcher.

Uses real exchange prices only. The watcher sleeps between polls, checks
active alerts from SQLite and sends a Telegram notification when a target is
hit. Alert triggers are deactivated immediately so the sender does not spam.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from loguru import logger

from cryptoforge_pro.db import Database
from cryptoforge_pro.market import MarketService
from cryptoforge_pro.telegram.format import format_alert_trigger


async def alert_loop(bot: Bot, market: MarketService, db: Database, interval: int = 60) -> None:
    logger.info("Alert watcher started (interval={}s)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await check_alerts(bot, market, db)
        except asyncio.CancelledError:
            logger.info("Alert watcher stopped")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alert watcher iteration failed: {}", exc)


async def check_alerts(bot: Bot, market: MarketService, db: Database) -> None:
    alerts = await db.get_active_alerts()
    if not alerts:
        return
    fired: list[tuple[dict, float, str]] = []
    for alert in alerts:
        symbol = (alert.get("symbol") or "").upper()
        try:
            price = await market.get_price(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Alert price check failed for {}: {}", symbol, exc)
            continue
        above = alert.get("target_above")
        below = alert.get("target_below")
        reason = ""
        if above and price >= float(above):
            reason = "цена достигла заданного уровня сверху"
            fired.append((alert, price, reason))
        elif below and price <= float(below):
            reason = "цена достигла заданного уровня снизу"
            fired.append((alert, price, reason))

    for alert, price, reason in fired:
        try:
            await db.deactivate_alert(int(alert["id"]))
            await bot.send_message(
                int(alert["chat_id"]),
                format_alert_trigger(alert, price, reason),
                parse_mode="HTML",
            )
            logger.info("Alert {} fired for {}", alert["id"], alert.get("symbol"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not send alert {}: {}", alert.get("id"), exc)
