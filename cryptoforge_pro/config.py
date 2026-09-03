"""Runtime configuration for CryptoForge Ultimate."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    telegram_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "BOT_TOKEN", "TG_TOKEN", "BOT_API_TOKEN"
        ),
    )
    admin_chat_ids: str = ""
    allowed_chat_ids: str = ""
    raw_chat_id: str = Field(default="", validation_alias="CHAT_ID")

    # The current engine is intentionally Bybit-only. Legacy exchange settings
    # are ignored so an old .env cannot accidentally select another venue.
    top_n_symbols: int = Field(default=100, ge=10, le=500)
    min_volume_usd_24h: float = Field(default=3_000_000, ge=0)
    http_timeout: float = Field(default=12.0, ge=2.0, le=60.0)
    data_dir: str = "data"
    log_level: str = "INFO"

    default_risk_profile: Literal["conservative", "balanced", "aggressive"] = "balanced"

    @field_validator("admin_chat_ids", "allowed_chat_ids", "raw_chat_id")
    @classmethod
    def _clean(cls, v: str) -> str:
        return v.strip()

    @staticmethod
    def _parse_ids(raw: str) -> set[int]:
        ids: set[int] = set()
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                ids.add(int(item))
            except ValueError:
                continue
        return ids

    @property
    def admin_ids(self) -> set[int]:
        ids = self._parse_ids(self.admin_chat_ids)
        if not ids and self.raw_chat_id:
            ids = self._parse_ids(self.raw_chat_id)
        return ids

    @property
    def allowed_ids(self) -> set[int]:
        ids = self._parse_ids(self.allowed_chat_ids)
        if not ids and self.raw_chat_id:
            ids = self._parse_ids(self.raw_chat_id)
        return ids


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
