"""Production entry point for CryptoForge Ultimate."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError

from cryptoforge_pro.config import get_settings
from cryptoforge_pro.ultimate_bot import Bybit, Scanner, Store
from cryptoforge_pro.ultimate_telegram_v2 import router, setup

log = logging.getLogger("cryptoforge")


async def run() -> None:
    s = get_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    token = s.telegram_token.strip()
    if not token:
        raise SystemExit(
            "Telegram token is missing. Set TELEGRAM_BOT_TOKEN (or TELEGRAM_TOKEN/BOT_TOKEN)."
        )

    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        me = await bot.get_me()
        log.info("Telegram authentication OK: @%s (%s)", me.username, me.id)
        # Polling cannot coexist with an active webhook. Remove it on startup;
        # this is safe for this single-worker Railway deployment.
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("Telegram webhook cleared; starting polling")
    except TelegramUnauthorizedError as exc:
        await bot.session.close()
        raise SystemExit(
            "Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN in Railway Variables."
        ) from exc
    except Exception:
        await bot.session.close()
        raise

    api = Bybit(s.http_timeout)
    scanner = Scanner(
        api,
        s.min_volume_usd_24h,
        max_candidates=max(60, s.top_n_symbols),
    )
    store = Store(os.path.join(s.data_dir, "ultimate.db"))
    await store.init()
    setup(scanner, store, s.allowed_ids)

    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await api.close()
        await bot.session.close()
        log.info("CryptoForge stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
