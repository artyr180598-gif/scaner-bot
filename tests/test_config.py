from __future__ import annotations

import pytest

from cryptopilot.config import Settings


def test_legacy_railway_variable_aliases(monkeypatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "legacy-token")
    monkeypatch.setenv("CHAT_ID", "123,456")
    monkeypatch.setenv("MIN_VOLUME_USD_24H", "3000000")
    monkeypatch.setenv("TOP_N_SYMBOLS", "100")
    monkeypatch.setenv("HTTP_TIMEOUT", "10")
    config = Settings(_env_file=None)

    assert config.telegram_bot_token == "legacy-token"
    assert config.allowed_chat_ids == frozenset({123, 456})
    assert config.min_volume_usdt == 3_000_000
    assert config.universe_size == 100
    assert config.http_timeout_seconds == 10


def test_auto_threshold_cannot_be_lower_than_manual() -> None:
    with pytest.raises(ValueError, match="MIN_AUTO_CONFIDENCE"):
        Settings(_env_file=None, min_manual_confidence=80, min_auto_confidence=70)
