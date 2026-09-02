"""
tests/conftest.py — общие фикстуры.

Тесты полностью офлайновые: биржи мокируются, рынок генерируется
`app.data.synthetic`. Сеть в тестах не нужна — их можно гонять в CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings  # noqa: E402
from app.data.synthetic import make_snapshot  # noqa: E402
from app.domain.models import Direction, MarketContext, Timeframe  # noqa: E402
from app.signals.engine import SignalEngine  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Настройки с ослабленными порогами: тесты проверяют логику, а не гейты."""
    s = Settings()
    s.min_confidence = 4.0
    s.min_rr = 1.1
    s.min_quote_volume_usd = 100_000
    s.data_dir = Path("data")
    return s


@pytest.fixture
def strict_settings() -> Settings:
    """Продуктовые настройки по умолчанию."""
    return Settings()


@pytest.fixture
def engine(settings) -> SignalEngine:
    return SignalEngine(settings)


@pytest.fixture
def bull_context() -> MarketContext:
    return MarketContext(
        btc_score=0.5, btc_trend="восходящий", btc_direction=Direction.LONG,
        breadth_24h_positive=0.65, median_change_24h=1.2, regime="риск-он")


@pytest.fixture
def bear_context() -> MarketContext:
    return MarketContext(
        btc_score=-0.5, btc_trend="нисходящий", btc_direction=Direction.SHORT,
        breadth_24h_positive=0.3, median_change_24h=-1.2, regime="риск-офф")


@pytest.fixture
def snapshot():
    return make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)


@pytest.fixture
def signal_timeframe() -> Timeframe:
    return Timeframe.H1
