from __future__ import annotations

import pytest

from cryptopilot.config import Settings


def test_legacy_railway_variable_aliases(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "legacy-token")
    monkeypatch.setenv("ADMIN_ID", "123,456")
    config = Settings(_env_file=None)

    assert config.telegram_bot_token == "legacy-token"
    assert config.allowed_chat_ids == frozenset({123, 456})


def test_auto_threshold_cannot_be_lower_than_manual() -> None:
    with pytest.raises(ValueError, match="MIN_AUTO_CONFIDENCE"):
        Settings(_env_file=None, min_manual_confidence=80, min_auto_confidence=70)
