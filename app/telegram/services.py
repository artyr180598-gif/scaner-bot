"""
app/telegram/services.py — сборка сервисов бота (composition root).

Здесь создаются все зависимости и связываются между собой. Telegram-хендлеры
получают готовый контейнер и ничего не конструируют сами — это позволяет
подменить любой сервис в тестах.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config.settings import Settings
from app.data.market import MarketDataService
from app.services.journal import SignalJournal
from app.services.news import NewsService
from app.services.scanner import ScannerService
from app.services.watchlist import Store
from app.signals.engine import SignalEngine

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BotServices:
    settings: Settings
    market: MarketDataService
    engine: SignalEngine
    scanner: ScannerService
    journal: SignalJournal
    store: Store
    news: NewsService

    async def close(self) -> None:
        await self.market.close()


async def create_services(settings: Settings,
                          market: Optional[MarketDataService] = None
                          ) -> BotServices:
    """Создаёт контейнер сервисов. ``market`` можно подменить (тесты, мок)."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    market = market or await MarketDataService.create(settings)
    engine = SignalEngine(settings)
    scanner = ScannerService(settings, market, engine)
    journal = SignalJournal(Path(settings.data_dir) / "journal.json")
    store = Store(Path(settings.data_dir) / "store.json")
    news = NewsService(api_key=settings.cryptocompare_api_key,
                       enabled=settings.news_enabled and bool(settings.cryptocompare_api_key))
    return BotServices(settings=settings, market=market, engine=engine,
                       scanner=scanner, journal=journal, store=store, news=news)
