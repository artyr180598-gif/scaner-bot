"""Shared context injected into aiogram handlers."""

from __future__ import annotations

from dataclasses import dataclass

from cryptoforge_pro.analysis.engine import Scanner, SignalEngine
from cryptoforge_pro.config import Settings
from cryptoforge_pro.data.exchanges import ExchangeRouter
from cryptoforge_pro.data.http import HttpSession
from cryptoforge_pro.db import Database
from cryptoforge_pro.market import MarketService


@dataclass(slots=True)
class BotContext:
    settings: Settings
    db: Database
    http: HttpSession
    exchanges: ExchangeRouter
    market: MarketService
    engine: SignalEngine
    scanner: Scanner
