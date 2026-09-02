"""app/data — доступ к рыночным данным (ccxt), кэш, синтетика для тестов."""

from app.data.cache import TTLCache  # noqa: F401
from app.data.exchanges import ExchangeGateway, create_gateways  # noqa: F401
from app.data.market import MarketDataService  # noqa: F401
