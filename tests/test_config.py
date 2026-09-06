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


def test_adaptive_defaults_are_conservative() -> None:
    config = Settings(_env_file=None)

    assert config.min_auto_confidence == 88
    assert config.min_auto_confidence_short == 90
    assert config.max_portfolio_risk_pct >= config.risk_per_trade_pct
    assert config.max_same_side_auto_signals <= config.max_auto_signals_per_scan
    assert config.min_early_auto_readiness >= config.min_early_readiness
    assert config.early_radar_enabled
    assert not config.early_auto_alerts
    assert config.smart_money_auto_scan_enabled
    assert config.prime_alerts_enabled
    assert config.prime_min_score >= 88
    assert config.max_auto_signals_per_scan == 1
    assert not config.early_auto_alerts
    assert config.flow_radar_enabled
    assert not config.flow_auto_alerts_enabled
    assert config.flow_watchlist_limit <= 8
    assert config.smart_money_scan_interval_seconds >= 120
    assert config.flow_min_alert_score >= 70
    assert config.flow_delta_ratio_threshold >= 0.10
    assert config.flow_volume_burst_ratio >= 1.0
    assert config.preferred_leverage <= config.max_leverage <= 3
