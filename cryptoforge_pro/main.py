"""Production entry point for CryptoForge Ultimate."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from cryptoforge_pro.config import get_settings
from cryptoforge_pro.ultimate_bot import Bybit, Scanner, Store
from cryptoforge_pro.ultimate_telegram_v2 import router, setup

log = logging.getLogger("cryptoforge")


async def run() -> None:
    s = get_settings()
    token = (s.telegram_token or "").strip()
    if not token:
        raise SystemExit(
            "Telegram token is missing. Set TELEGRAM_BOT_TOKEN or TELEGRAM_TOKEN in Railway Variables."
        )

    logging.basicConfig(
        level=getattr(logging, str(s.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    api = Bybit(s.http_timeout)
    scanner = Scanner(api, s.min_volume_usd_24h, max_candidates=max(60, s.top_n_symbols))
    store = Store(os.path.join(s.data_dir, "ultimate.db"))
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    setup(scanner, store, s.allowed_ids)

    try:
        me = await bot.get_me()
        log.info("CryptoForge starting as @%s (id=%s)", me.username, me.id)
        log.info("Allowed chat IDs configured: %s", len(s.allowed_ids))
        await store.init()

        # Ensure an old webhook does not block long polling after a Railway restart.
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("Telegram webhook cleared; starting long polling")
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            handle_as_tasks=True,
        )
    except Exception:
        log.exception("CryptoForge failed during startup/polling")
        raise
    finally:
        await bot.session.close()
        await api.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
