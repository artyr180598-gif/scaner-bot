"""
Настройки приложения.

Загружаются из переменных окружения. Валидируются при старте (см. Settings.validate).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Конфигурация бота Крис."""

    # ── Telegram ───────────────────────────────────────────────
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    admin_chat_ids: List[int] = field(default_factory=list)
    allowed_chat_ids: List[int] = field(default_factory=list)
    dry_run: bool = True

    # ── Биржи ──────────────────────────────────────────────────
    exchanges: List[str] = field(default_factory=lambda: ["binance", "bybit"])
    quote_currencies: List[str] = field(default_factory=lambda: ["USDT"])
    # Сколько топ-монет по объёму сканировать
    top_n_symbols: int = 100

    # ── Таймфреймы ─────────────────────────────────────────────
    # Базовый таймфрейм для сканера; старшие — для подтверждения.
    base_timeframe: str = "1h"
    analysis_timeframes: List[str] = field(default_factory=lambda: ["1h", "4h", "1d"])
    # Сколько свечей тянуть на каждом ТФ
    candles_limit: int = 300

    # ── Движок сигналов ────────────────────────────────────────
    min_confidence: float = 75.0  # жёсткий фильтр по ТЗ
    min_volume_usd_24h: float = 5_000_000.0  # анти-мусор
    min_atr_pct: float = 0.3
    max_atr_pct: float = 12.0
    # Анти-«погоня за движением» — актив уже дал импульс >N% за окно
    anti_chase_window_bars: int = 12
    anti_chase_max_pct: float = 8.0

    # ── Веса групп факторов (для confidence) ───────────────────
    # Схлопываем похожие сигналы в независимые группы (урок v4 BRAIN.md).
    weights_trend: float = 1.0
    weights_momentum: float = 0.9
    weights_volume: float = 0.8
    weights_volatility: float = 0.6
    weights_structure: float = 0.7
    weights_patterns: float = 0.5

    # ── LLM (опционально, для блока «Логика входа») ────────────
    llm_enabled: bool = False
    llm_provider: str = "openai"  # openai | anthropic | local
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_max_tokens: int = 220
    # Если LLM недоступна — используется детерминированный шаблон.

    # ── Логирование/хранение ────────────────────────────────────
    log_level: str = "INFO"
    data_dir: str = "data"
    signals_db: str = "signals.db"

    def validate(self) -> None:
        """Проверить инварианты. Бросает ValueError."""
        if not self.telegram_token:
            raise ValueError("TELEGRAM_TOKEN is required")

        if not self.exchanges:
            raise ValueError("at least one exchange is required")

        # Базовый ТФ должен присутствовать в списке анализа.
        if self.base_timeframe not in self.analysis_timeframes:
            raise ValueError(
                f"BASE_TIMEFRAME={self.base_timeframe} must be in "
                f"ANALYSIS_TIMEFRAMES={self.analysis_timeframes}"
            )

        if not (0.0 <= self.min_confidence <= 100.0):
            raise ValueError("MIN_CONFIDENCE must be in [0, 100]")

        if self.min_volume_usd_24h < 0:
            raise ValueError("MIN_VOLUME_USD_24H must be >= 0")

        if self.candles_limit < 50:
            raise ValueError("CANDLES_LIMIT must be >= 50")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Синглтон настроек. Парсит ENV один раз."""
    return Settings(
        telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
        admin_chat_ids=[int(x) for x in _list("ADMIN_CHAT_IDS") if x.lstrip("-").isdigit()],
        allowed_chat_ids=[int(x) for x in _list("ALLOWED_CHAT_IDS") if x.lstrip("-").isdigit()],
        dry_run=_bool("DRY_RUN", True),
        exchanges=_list("EXCHANGES", ["binance", "bybit"]) or ["binance"],
        quote_currencies=_list("QUOTE_CURRENCIES", ["USDT"]) or ["USDT"],
        top_n_symbols=_int("TOP_N_SYMBOLS", 100),
        base_timeframe=os.getenv("BASE_TIMEFRAME", "1h"),
        analysis_timeframes=_list("ANALYSIS_TIMEFRAMES", ["1h", "4h", "1d"]) or ["1h", "4h", "1d"],
        candles_limit=_int("CANDLES_LIMIT", 300),
        min_confidence=_float("MIN_CONFIDENCE", 75.0),
        min_volume_usd_24h=_float("MIN_VOLUME_USD_24H", 5_000_000.0),
        min_atr_pct=_float("MIN_ATR_PCT", 0.3),
        max_atr_pct=_float("MAX_ATR_PCT", 12.0),
        anti_chase_window_bars=_int("ANTI_CHASE_WINDOW_BARS", 12),
        anti_chase_max_pct=_float("ANTI_CHASE_MAX_PCT", 8.0),
        llm_enabled=_bool("LLM_ENABLED", False),
        llm_provider=os.getenv("LLM_PROVIDER", "openai"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        data_dir=os.getenv("DATA_DIR", "data"),
        signals_db=os.getenv("SIGNALS_DB", "signals.db"),
    )
