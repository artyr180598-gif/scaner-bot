"""
tests/test_settings.py — конфигурация: значения, валидация, риск-профили.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import RISK_PRESETS, Settings
from app.domain.models import RiskProfile, Timeframe
from app.utils.errors import ConfigError

# Все переменные, которые читает Settings.from_env — чистим окружение в тестах,
# чтобы локальный .env или CI не влияли на результат.
ENV_KEYS = [
    "TELEGRAM_BOT_TOKEN", "ADMIN_CHAT_IDS", "ALLOWED_CHAT_IDS", "TELEGRAM_PARSE_MODE",
    "EXCHANGES", "DERIVATIVES_EXCHANGES", "QUOTE", "MARKET_TYPE", "REQUEST_TIMEOUT_MS",
    "RATE_LIMIT_CONCURRENCY", "RATE_LIMIT_MIN_INTERVAL", "MAX_UNIVERSE",
    "MIN_QUOTE_VOLUME_USD", "EXCLUDE_SYMBOLS", "INCLUDE_SYMBOLS",
    "EXCLUDE_LEVERAGED_TOKENS", "BASE_TIMEFRAME", "ANALYSIS_TIMEFRAMES",
    "SIGNAL_TIMEFRAME", "BARS_BASE", "BARS_DAILY", "CACHE_TTL_SECONDS",
    "MAX_STALENESS_SECONDS", "MIN_BARS_REQUIRED", "PRESCREEN_CANDIDATES",
    "DEEP_ANALYSIS_CONCURRENCY", "MIN_COMPRESSION_PERCENTILE", "MAX_RUN_ZSCORE",
    "MAX_DISTANCE_HIGH_PCT", "VOLUME_ANOMALYZE_Z", "VOLUME_ANOMALY_Z", "RISK_PROFILE",
    "DEPOSIT_USD", "RISK_PER_TRADE_PCT", "MAX_LEVERAGE", "MIN_CONFIDENCE", "MIN_RR",
    "MAX_OPEN_SIGNALS", "WAIT_THRESHOLD", "ANTI_CHASE_ATR",
    "REQUIRE_STRUCTURE_ALIGNMENT", "SCAN_INTERVAL_MIN", "AUTO_PUSH",
    "PUSH_MIN_CONFIDENCE", "TOP_N", "WATCHLIST_COOLDOWN_MIN", "STARTUP_SCAN",
    "NEWS_ENABLED", "CRYPTOCOMPARE_API_KEY", "NEWS_MAX_ITEMS", "SOCIAL_ENABLED",
    "LOG_LEVEL", "DATA_DIR", "DRY_RUN",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Значения по умолчанию
# ---------------------------------------------------------------------------

def test_defaults_are_sane():
    s = Settings()
    assert s.max_universe == 250
    assert s.min_quote_volume_usd == 3_000_000
    assert s.base_timeframe is Timeframe.H1
    assert s.signal_timeframe is Timeframe.H1
    assert [t.value for t in s.analysis_timeframes] == ["1h", "4h", "1d"]
    assert s.bars_base == 720
    assert s.prescreen_candidates == 35
    assert s.min_confidence == 5.5
    assert s.min_rr == 1.6
    assert s.scan_interval_minutes == 30
    assert s.push_min_confidence == 7.0
    assert s.quote == "USDT"
    assert s.exchanges == ["binance", "bybit", "okx"]
    assert s.news_enabled is False


def test_defaults_pass_validation():
    Settings().validate()


def test_data_dir_is_path():
    assert isinstance(Settings().data_dir, Path)


# ---------------------------------------------------------------------------
# Валидация
# ---------------------------------------------------------------------------

def test_validate_rejects_too_few_bars():
    s = Settings()
    s.bars_base = 50
    with pytest.raises(ConfigError, match="BARS_BASE"):
        s.validate()


def test_validate_rejects_base_timeframe_older_than_analysis():
    s = Settings()
    s.base_timeframe = Timeframe.H4
    with pytest.raises(ConfigError, match="BASE_TIMEFRAME"):
        s.validate()


def test_validate_rejects_signal_timeframe_outside_analysis_list():
    s = Settings()
    s.signal_timeframe = Timeframe.M5
    with pytest.raises(ConfigError, match="SIGNAL_TIMEFRAME"):
        s.validate()


def test_validate_rejects_bad_risk_per_trade():
    for bad in (0.0, -1.0, 10.5):
        s = Settings()
        s.risk_per_trade_pct = bad
        with pytest.raises(ConfigError, match="RISK_PER_TRADE_PCT"):
            s.validate()


def test_validate_rejects_rr_below_one():
    s = Settings()
    s.min_rr = 0.9
    with pytest.raises(ConfigError, match="MIN_RR"):
        s.validate()


def test_validate_rejects_leverage_below_one():
    s = Settings()
    s.max_leverage = 0.5
    with pytest.raises(ConfigError, match="MAX_LEVERAGE"):
        s.validate()


def test_validate_rejects_confidence_out_of_scale():
    s = Settings()
    s.min_confidence = 11.0
    with pytest.raises(ConfigError, match="MIN_CONFIDENCE"):
        s.validate()


def test_validate_rejects_too_frequent_scan():
    s = Settings()
    s.scan_interval_minutes = 2
    with pytest.raises(ConfigError, match="SCAN_INTERVAL_MIN"):
        s.validate()


def test_validate_collects_all_problems():
    s = Settings()
    s.bars_base = 10
    s.min_rr = 0.5
    with pytest.raises(ConfigError) as exc:
        s.validate()
    message = str(exc.value)
    assert "BARS_BASE" in message and "MIN_RR" in message


# ---------------------------------------------------------------------------
# Окружение и профили риска
# ---------------------------------------------------------------------------

def test_from_env_reads_and_converts_types(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("MAX_UNIVERSE", "42")
    monkeypatch.setenv("MIN_CONFIDENCE", "7.25")
    monkeypatch.setenv("ANALYSIS_TIMEFRAMES", "1h,4h,1d")
    monkeypatch.setenv("SIGNAL_TIMEFRAME", "4h")
    monkeypatch.setenv("EXCHANGES", "Binance, Bybit")
    monkeypatch.setenv("QUOTE", "usdt")
    monkeypatch.setenv("NEWS_ENABLED", "да")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "111, 222")
    monkeypatch.setenv("DATA_DIR", "/tmp/adv")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    s = Settings.from_env()
    assert s.telegram_bot_token == "123:abc"
    assert s.max_universe == 42
    assert s.min_confidence == 7.25
    assert [t.value for t in s.analysis_timeframes] == ["1h", "4h", "1d"]
    assert s.signal_timeframe is Timeframe.H4
    assert s.exchanges == ["binance", "bybit"]
    assert s.quote == "USDT"
    assert s.news_enabled is True
    assert s.admin_chat_ids == [111, 222]
    assert s.data_dir == Path("/tmp/adv")
    assert s.log_level == "DEBUG"


def test_from_env_validates(monkeypatch):
    monkeypatch.setenv("BARS_BASE", "10")
    with pytest.raises(ConfigError):
        Settings.from_env()


@pytest.mark.parametrize("profile", ["conservative", "moderate", "aggressive"])
def test_risk_profile_preset_applies(monkeypatch, profile):
    monkeypatch.setenv("RISK_PROFILE", profile)
    s = Settings.from_env()
    expected = RISK_PRESETS[RiskProfile(profile)]
    assert s.risk_profile is RiskProfile(profile)
    assert s.min_confidence == expected["min_confidence"]
    assert s.min_rr == expected["min_rr"]
    assert s.risk_per_trade_pct == expected["risk_per_trade_pct"]
    assert s.max_leverage == expected["max_leverage"]
    assert s.preset()["stop_atr_mult"] == expected["stop_atr_mult"]


def test_explicit_env_overrides_profile(monkeypatch):
    monkeypatch.setenv("RISK_PROFILE", "conservative")
    monkeypatch.setenv("MIN_CONFIDENCE", "4.2")
    assert Settings.from_env().min_confidence == 4.2


def test_conservative_is_stricter_than_aggressive():
    strict = RISK_PRESETS[RiskProfile.CONSERVATIVE]
    loose = RISK_PRESETS[RiskProfile.AGGRESSIVE]
    assert strict["min_confidence"] > loose["min_confidence"]
    assert strict["min_rr"] > loose["min_rr"]
    assert strict["risk_per_trade_pct"] < loose["risk_per_trade_pct"]


# ---------------------------------------------------------------------------
# Представление
# ---------------------------------------------------------------------------

def test_describe_mentions_key_parameters():
    text = Settings().describe()
    assert "binance" in text
    assert "USDT" in text
    assert "1h" in text


def test_to_dict_is_json_friendly():
    import json

    data = Settings().to_dict()
    assert data["base_timeframe"] == "1h"
    assert data["risk_profile"] == "moderate"
    assert data["analysis_timeframes"] == ["1h", "4h", "1d"]
    assert data["data_dir"] is not None
    json.dumps({k: str(v) for k, v in data.items()})


# ---------------------------------------------------------------------------
# Документация должна совпадать с кодом
# ---------------------------------------------------------------------------

def test_env_example_documents_every_key():
    """Каждая переменная, которую читает Settings, описана в .env.example."""
    import re

    root = Path(__file__).resolve().parent.parent
    source = (root / "app" / "config" / "settings.py").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")

    used = set(re.findall(r'_(?:get|bool|int|float|list|tf_list)\(\s*"([A-Z][A-Z0-9_]+)"',
                          source.replace("\n", "")))
    assert len(used) > 40, f"регулярка нашла подозрительно мало ключей: {len(used)}"
    # Документированными считаем и закомментированные строки вида `# KEY=...`.
    documented = {line.split("=", 1)[0].lstrip("# ").strip()
                  for line in example.splitlines() if "=" in line}
    missing = used - documented
    assert not missing, f"не описаны в .env.example: {sorted(missing)}"


def test_env_example_parses_into_valid_settings(monkeypatch):
    """.env.example — рабочий файл, а не набор опечаток."""
    root = Path(__file__).resolve().parent.parent
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        monkeypatch.setenv(key.strip(), value.strip())
    settings = Settings.from_env()
    assert settings.max_universe == 250
    assert settings.min_confidence == 5.5
    assert settings.data_dir == Path("data")
