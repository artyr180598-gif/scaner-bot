"""
Бэктестер: историческая проверка стратегии.

Использует данные OKX (через fetch_page платформы или прямой HTTPS).
Идём по свечам, на каждой «сейчас» запускаем пайплайн сигналов, симулируем
вход по плану, выход по TP/SL/таймауту.

Без бэктестера мы повторим ошибку v4 (см. BRAIN.md): будем обещать
«уверенность 75% = прибыль» без доказательств.
"""
from .data_loader import fetch_okx_candles, candles_from_okx
from .engine import BacktestEngine, BacktestResult, Trade

__all__ = [
    "fetch_okx_candles",
    "candles_from_okx",
    "BacktestEngine",
    "BacktestResult",
    "Trade",
]
