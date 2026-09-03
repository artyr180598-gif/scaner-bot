"""Production entry point for CryptoForge Ultimate."""
from __future__ import annotations

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from cryptoforge_pro.config import get_settings
from cryptoforge_pro.ultimate_bot import Bybit, Scanner, Store
from cryptoforge_pro.ultimate_telegram import router, setup


async def run() -> None:
    s = get_settings()
    if not s.telegram_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN or TELEGRAM_TOKEN")

    api = Bybit(s.http_timeout)
    history_path = os.path.join(s.data_dir, "ultimate.db")
    scanner = Scanner(api, s.min_volume_usd_24h, max_candidates=max(60, s.top_n_symbols))
    store = Store(history_path)
    await store.init()
    setup(scanner, store, s.allowed_ids)

    bot = Bot(s.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()
        await api.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
