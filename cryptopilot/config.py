from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_ignore_empty=True,
        populate_by_name=True,
        extra="ignore",
    )

    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_TOKEN",
            "BOT_TOKEN",
            "TG_TOKEN",
            "BOT_API_TOKEN",
        ),
    )
    telegram_chat_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_USER_ID",
            "ALLOWED_CHAT_IDS",
            "ADMIN_CHAT_IDS",
            "CHAT_ID",
            "ADMIN_ID",
        ),
    )
    exchange: Literal["bybit", "binance"] = "bybit"
    bybit_base_url: str = "https://api.bybit.com"
    binance_base_url: str = "https://fapi.binance.com"

    scan_interval_seconds: int = Field(default=900, ge=300, le=86_400)
    run_scan_on_startup: bool = True
    min_volume_usdt: float = Field(
        default=20_000_000,
        ge=0,
        validation_alias=AliasChoices("MIN_VOLUME_USDT", "MIN_VOLUME_USD_24H"),
    )
    max_spread_bps: float = Field(default=12, gt=0, le=100)
    universe_size: int = Field(
        default=80,
        ge=10,
        le=250,
        validation_alias=AliasChoices("UNIVERSE_SIZE", "TOP_N_SYMBOLS"),
    )
    shortlist_size: int = Field(default=12, ge=3, le=40)
    early_shortlist_size: int = Field(default=10, ge=3, le=40)
    request_concurrency: int = Field(default=8, ge=1, le=20)
    timeframes: str = "15,60,240"
    min_auto_confidence: int = Field(default=88, ge=55, le=95)
    min_auto_confidence_short: int = Field(default=90, ge=55, le=95)
    min_manual_confidence: int = Field(default=72, ge=50, le=95)
    alert_cooldown_minutes: int = Field(default=180, ge=15, le=10_080)
    signal_expiry_minutes: int = Field(default=240, ge=30, le=2_880)
    max_auto_signals_per_scan: int = Field(default=1, ge=1, le=10)
    max_same_side_auto_signals: int = Field(default=1, ge=1, le=5)
    max_portfolio_risk_pct: float = Field(default=1.0, gt=0, le=5)
    excluded_symbols: str = "USDCUSDT,FDUSDUSDT,TUSDUSDT,USDEUSDT"

    min_primary_adx: float = Field(default=18.0, ge=0, le=60)
    min_efficiency_ratio: float = Field(default=0.14, ge=0, le=1)
    min_ema_gap_atr: float = Field(default=0.08, ge=0, le=5)
    max_countertrend_dmi: float = Field(default=5.0, ge=0, le=100)
    max_atr_regime_ratio: float = Field(default=2.8, ge=1, le=10)
    relative_strength_filter: bool = True
    neutral_regime_confidence_penalty: int = Field(default=2, ge=0, le=10)
    market_microstructure_enabled: bool = True

    paper_tracking_enabled: bool = True
    paper_max_holding_hours: int = Field(default=72, ge=4, le=336)
    paper_one_way_cost_bps: float = Field(default=6.0, ge=0, le=100)
    calibration_min_samples: int = Field(default=30, ge=10, le=500)
    calibration_lookback: int = Field(default=100, ge=20, le=1000)
    weak_edge_confidence_penalty: int = Field(default=2, ge=0, le=10)

    early_radar_enabled: bool = True
    live_radar_enabled: bool = True
    squeeze_lab_enabled: bool = True
    live_watchlist_interval_seconds: int = Field(default=300, ge=60, le=3600)
    early_auto_alerts: bool = False
    min_early_readiness: int = Field(default=68, ge=50, le=95)
    min_early_auto_readiness: int = Field(default=80, ge=55, le=95)
    early_alert_cooldown_minutes: int = Field(default=360, ge=30, le=10_080)
    early_setup_expiry_minutes: int = Field(default=720, ge=60, le=2_880)
    early_min_squeeze_bars: int = Field(default=2, ge=1, le=20)

    smart_money_auto_scan_enabled: bool = True
    smart_money_scan_interval_seconds: int = Field(default=300, ge=120, le=3600)
    flow_radar_enabled: bool = True
    flow_auto_alerts_enabled: bool = False
    flow_watchlist_limit: int = Field(default=8, ge=3, le=20)
    flow_min_notional_60s: float = Field(default=35_000, ge=1_000, le=50_000_000)
    flow_delta_ratio_threshold: float = Field(default=0.20, ge=0.05, le=0.8)
    flow_volume_burst_ratio: float = Field(default=1.5, ge=1.0, le=10.0)
    flow_min_oi_change_pct_2m: float = Field(default=0.12, ge=0.0, le=20.0)
    flow_min_alert_score: int = Field(default=84, ge=50, le=95)
    flow_alert_cooldown_minutes: int = Field(default=360, ge=15, le=1440)
    flow_global_cooldown_minutes: int = Field(default=180, ge=30, le=1440)
    flow_max_alerts_per_day: int = Field(default=2, ge=1, le=20)
    flow_max_directional_funding_pct: float = Field(default=0.08, ge=0.01, le=1.0)
    flow_validation_enabled: bool = True
    flow_validation_window_minutes: int = Field(default=45, ge=10, le=240)
    flow_validation_min_samples: int = Field(default=20, ge=5, le=500)

    prime_alerts_enabled: bool = True
    prime_min_score: int = Field(default=88, ge=70, le=98)
    prime_global_cooldown_minutes: int = Field(default=180, ge=30, le=1440)
    prime_symbol_cooldown_minutes: int = Field(default=720, ge=60, le=10_080)
    prime_max_alerts_per_day: int = Field(default=3, ge=1, le=20)
    prime_min_trigger_distance_pct: float = Field(default=0.25, ge=0.05, le=2.0)
    prime_max_trigger_distance_pct: float = Field(default=1.20, ge=0.20, le=5.0)
    prime_max_price_move_60s_pct: float = Field(default=0.12, ge=0.02, le=2.0)
    prime_max_directional_move_15m_pct: float = Field(default=0.35, ge=0.05, le=3.0)
    prime_spot_confirmation_enabled: bool = True
    prime_min_spot_taker_ratio: float = Field(default=0.60, ge=0.50, le=0.90)
    prime_min_spot_book_imbalance: float = Field(default=0.05, ge=0.0, le=0.80)
    prime_min_spot_confirmations: int = Field(default=2, ge=1, le=3)
    prime_max_directional_perp_basis_bps: float = Field(default=20.0, ge=1.0, le=200.0)
    prime_min_spot_block_notional: float = Field(default=50_000, ge=0, le=100_000_000)

    liquidity_intelligence_enabled: bool = True
    liquidity_orderbook_watch_limit: int = Field(default=4, ge=1, le=10)
    prime_min_persistent_wall_ratio: float = Field(default=2.5, ge=1.5, le=20.0)
    prime_min_wall_persistence_seconds: int = Field(default=10, ge=3, le=120)
    prime_min_replenishment_notional: float = Field(default=25_000, ge=0, le=100_000_000)
    prime_max_directional_liquidation_ratio: float = Field(default=0.25, ge=0.05, le=2.0)

    prime_cross_exchange_enabled: bool = True
    prime_cross_exchange_min_confirmations: int = Field(default=2, ge=1, le=4)
    prime_cross_exchange_max_price_divergence_bps: float = Field(
        default=35.0, ge=5.0, le=200.0
    )
    prime_entry_expiry_minutes: int = Field(default=90, ge=15, le=360)
    prime_min_plan_rr: float = Field(default=2.0, ge=1.2, le=5.0)
    prime_max_stop_pct: float = Field(default=2.5, ge=0.5, le=6.0)
    prime_risk_multiplier: float = Field(default=0.5, gt=0, le=1.0)
    prime_max_leverage: int = Field(default=2, ge=1, le=3)

    account_equity_usdt: float = Field(default=1000, gt=0)
    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=2)
    max_position_pct: float = Field(default=25, gt=0, le=100)
    min_risk_reward: float = Field(default=1.8, ge=1, le=5)
    preferred_leverage: int = Field(default=2, ge=1, le=3)
    max_leverage: int = Field(default=3, ge=1, le=3)

    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    port: int = Field(default=8080, ge=1, le=65_535)
    http_timeout_seconds: float = Field(
        default=15,
        ge=3,
        le=60,
        validation_alias=AliasChoices("HTTP_TIMEOUT_SECONDS", "HTTP_TIMEOUT"),
    )

    @field_validator("exchange", mode="before")
    @classmethod
    def normalize_exchange(cls, value: object) -> str:
        return str(value).strip().lower()

    @field_validator("bybit_base_url", "binance_base_url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def validate_thresholds(self) -> Settings:
        if self.shortlist_size > self.universe_size:
            raise ValueError("SHORTLIST_SIZE cannot exceed UNIVERSE_SIZE")
        if self.early_shortlist_size > self.universe_size:
            raise ValueError("EARLY_SHORTLIST_SIZE cannot exceed UNIVERSE_SIZE")
        if self.min_auto_confidence < self.min_manual_confidence:
            raise ValueError("MIN_AUTO_CONFIDENCE must be >= MIN_MANUAL_CONFIDENCE")
        if self.min_auto_confidence_short < self.min_manual_confidence:
            raise ValueError("MIN_AUTO_CONFIDENCE_SHORT must be >= MIN_MANUAL_CONFIDENCE")
        if self.max_same_side_auto_signals > self.max_auto_signals_per_scan:
            raise ValueError("MAX_SAME_SIDE_AUTO_SIGNALS cannot exceed MAX_AUTO_SIGNALS_PER_SCAN")
        if self.max_portfolio_risk_pct < self.risk_per_trade_pct:
            raise ValueError("MAX_PORTFOLIO_RISK_PCT must be >= RISK_PER_TRADE_PCT")
        if self.min_early_auto_readiness < self.min_early_readiness:
            raise ValueError("MIN_EARLY_AUTO_READINESS must be >= MIN_EARLY_READINESS")
        if self.preferred_leverage > self.max_leverage:
            raise ValueError("PREFERRED_LEVERAGE cannot exceed MAX_LEVERAGE")
        if self.prime_max_leverage > self.max_leverage:
            raise ValueError("PRIME_MAX_LEVERAGE cannot exceed MAX_LEVERAGE")
        return self

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        values: set[int] = set()
        for raw in self.telegram_chat_id.replace(";", ",").split(","):
            item = raw.strip()
            if item:
                values.add(int(item))
        return frozenset(values)

    @property
    def timeframe_list(self) -> tuple[str, ...]:
        allowed = {"5", "15", "30", "60", "120", "240", "D"}
        result = tuple(x.strip().upper() for x in self.timeframes.split(",") if x.strip())
        if len(result) < 3 or any(item not in allowed for item in result):
            raise ValueError("TIMEFRAMES must contain at least three supported intervals")
        return result

    @property
    def excluded_symbol_set(self) -> frozenset[str]:
        return frozenset(x.strip().upper() for x in self.excluded_symbols.split(",") if x.strip())

    @property
    def database_path(self) -> Path:
        return self.data_dir / "cryptopilot.sqlite3"

    def prepare_runtime(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
