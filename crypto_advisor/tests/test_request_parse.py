"""Тесты парсера свободного запроса."""

from __future__ import annotations

from crypto_advisor.core.domain.query import UserRequest
from crypto_advisor.core.domain.signal import Direction


def test_direction_long():
    r = UserRequest.from_text("агрессивный лонг на 1h")
    assert r.wants_direction == Direction.LONG
    assert r.risk_profile == "aggressive"
    assert r.timeframe == "1h"


def test_direction_short_latin():
    r = UserRequest.from_text("short on 4h, stable coins")
    assert r.wants_direction == Direction.SHORT
    assert r.timeframe == "4h"


def test_conservative_profile():
    r = UserRequest.from_text("консервативные стабильные монеты")
    assert r.risk_profile == "conservative"


def test_auto_direction():
    r = UserRequest.from_text("сбалансированный")
    assert r.wants_direction is None


def test_volume_and_atr():
    r = UserRequest.from_text("объём от 10 млн, волатильность до 6%")
    assert r.min_volume_usd_24h >= 10_000_000
    assert r.max_atr_pct == 6.0


def test_symbol_detected():
    r = UserRequest.from_text("анализ SOL")
    assert "SOL" in r.symbols


def test_keyword_not_noise():
    # "агрессивный" — это подсказка про профиль, а не тема.
    r = UserRequest.from_text("агрессивный лонг")
    assert "агрессивный" not in r.keyword


def test_parse_never_crashes_on_garbage():
    r = UserRequest.from_text("!!!")  # type: ignore[arg-type]
    assert r.direction == Direction.NEUTRAL
    assert r.timeframe == "1h"
