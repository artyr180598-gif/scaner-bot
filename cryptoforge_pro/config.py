"""Configuration for CryptoForge Pro.

Environment variables are loaded via pydantic-settings. Real environment
variables always override values from .env on Railway / VPS.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ParseModeChoice(str, Enum):
    html = "HTML"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # Telegram -----------------------------------------------------------------
    # Railway uses TELEGRAM_BOT_TOKEN; local envs often use TELEGRAM_TOKEN.
    # All common names are accepted through an alias, so deploy works unchanged.
    telegram_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TELEGRAM_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "BOT_TOKEN",
            "TG_TOKEN",
            "BOT_API_TOKEN",
        ),
        description="Bot token from @BotFather",
    )
    bot_username: str = Field(default="", description="Optional @username of the bot")

    # Access control ------------------------------------------------------------
    # Railway often exposes CHAT_ID for the allowed chat. Standard variables
    # ALLOWED_CHAT_IDS / ADMIN_CHAT_IDS keep full control. If CHAT_ID is set
    # and standard vars are empty, it is used as both admin and allowed chat.
    admin_chat_ids: str = Field(default="", description="Comma separated admin chat ids")
    allowed_chat_ids: str = Field(default="", description="Empty = everyone, comma separated otherwise")
    raw_chat_id: str = Field(
        default="",
        validation_alias="CHAT_ID",
        description="Railway CHAT_ID — single allowed chat, optional",
    )

    # Market / exchanges ---------------------------------------------------------
    exchanges: str = Field(default="binance,bybit", description="Comma separated exchanges")
    quote_currencies: str = Field(default="USDT", description="Quote currencies, comma separated")
    top_n_symbols: int = Field(default=100, ge=10, le=500)
    base_timeframe: str = Field(default="1h")
    analysis_timeframes: str = Field(default="15m,1h,4h,1d")
    candles_limit: int = Field(default=300, ge=100, le=1000)

    # Signal engine -----------------------------------------------------------------
    min_confidence: int = Field(default=60, ge=40, le=95)
    min_volume_usd_24h: float = Field(default=3_000_000, ge=0)
    min_atr_pct: float = Field(default=0.3, ge=0.0)
    max_atr_pct: float = Field(default=12.0, gt=0)
    anti_chase_window_bars: int = Field(default=12, ge=1, le=100)
    anti_chase_max_pct: float = Field(default=8.0, gt=0)
    max_matches: int = Field(default=8, ge=1, le=25)
    top_scans: int = Field(default=3, ge=1, le=10)
    confidence_bump_for_overy_strong: int = Field(default=3, ge=0, le=10)

    # Coinglass -------------------------------------------------------------------
    coinglass_api_key: str = Field(default="")
    coinglass_base_url: str = Field(default="https://open-api-v4.coinglass.com")

    # News --------------------------------------------------------------------------
    cryptopanic_api_key: str = Field(default="")
    news_language: str = Field(default="en")

    # Risk defaults ------------------------------------------------------------------
    default_risk_profile: Literal["conservative", "balanced", "aggressive"] = "balanced"
    risk_profiles: str = Field(default="conservative:58,balanced:62,aggressive:67")
    max_entries_per_signal: int = Field(default=1, ge=1, le=3)

    # Storage / logging -----------------------------------------------------------------
    signals_db: str = Field(default="data/signals.db")
    data_dir: str = Field(default="data")
    log_level: str = Field(default="INFO")

    # Alerts / watchers -------------------------------------------------------------------
    alert_check_interval_seconds: int = Field(default=60, ge=15, le=600)
    alert_price_window_pct: float = Field(default=1.5, ge=0.1, le=25.0)

    # HTTP -----------------------------------------------------------------------------
    http_timeout: float = Field(default=10.0, ge=2.0, le=60.0)
    cache_ttl_seconds: int = Field(default=45, ge=5, le=600)

    @field_validator("admin_chat_ids", "allowed_chat_ids", "raw_chat_id")
    @classmethod
    def _split_ids(cls, v: str) -> str:
        return v.strip()

    @property
    def admin_ids(self) -> set[int]:
        ids = self._parse_ids(self.admin_chat_ids)
        if not ids and self.raw_chat_id.strip():
            ids = self._parse_ids(self.raw_chat_id)
        return ids

    @property
    def allowed_ids(self) -> set[int]:
        ids = self._parse_ids(self.allowed_chat_ids)
        if not ids and self.raw_chat_id.strip():
            ids = self._parse_ids(self.raw_chat_id)
        return ids

    @property
    def exchange_list(self) -> list[str]:
        return [x.strip().lower() for x in self.exchanges.split(",") if x.strip()]

    @property
    def quote_list(self) -> list[str]:
        return [x.strip().upper() for x in self.quote_currencies.split(",") if x.strip()]

    @property
    def timeframe_list(self) -> list[str]:
        return [x.strip().lower() for x in self.analysis_timeframes.split(",") if x.strip()]

    @property
    def risk_thresholds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.risk_profiles.split(","):
            if ":" not in item:
                continue
            name, val = item.split(":", 1)
            try:
                out[name.strip().lower()] = int(float(val.strip()))
            except ValueError:
                continue
        return out

    @staticmethod
    def _parse_ids(raw: str) -> set[int]:
        if not raw.strip():
            return set()
        ids: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                pass
        return ids


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
