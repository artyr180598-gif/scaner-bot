"""Общие фикстуры для тестов (офлайн, без Telegram и сети)."""

from __future__ import annotations

import pytest

from ..config.settings import Settings
from ..data.exchange import get_synthetic
from ..scanner.engine import ScannerEngine


def make_settings() -> Settings:
    return Settings(
        telegram_token="000000000:TESTTOKENPLACEHOLDERPLACEHOLDER00",
        exchanges=[],
        dry_run=True,
    )


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def engine(settings):
    gw = get_synthetic("synthetic")
    e = ScannerEngine(settings=settings, gateway=gw)
    yield e
