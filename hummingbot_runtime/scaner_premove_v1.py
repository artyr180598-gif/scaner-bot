"""Hummingbot V2 controller for Scaner's PRE-MOVE research hypothesis.

This controller is deliberately candle-only. It is a research baseline for Hummingbot
backtesting and does not attempt to reproduce Scaner's live Spot/OI/order-book PRIME stack.
"""

from typing import List

import pandas as pd
from pydantic import Field

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase as _DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase as _DirectionalTradingControllerConfigBase,
)


class AScanerPremoveV1ControllerConfig(_DirectionalTradingControllerConfigBase):
    controller_name: str = "scaner_premove_v1"

    candles_connector: str | None = Field(default=None)
    candles_trading_pair: str | None = Field(default=None)
    interval: str = Field(default="15m")
    range_length: int = Field(default=20, ge=10, le=100)
    ema_fast: int = Field(default=20, ge=5, le=100)
    ema_slow: int = Field(default=50, ge=10, le=200)
    max_range_pct: float = Field(default=0.045, gt=0.005, le=0.20)
    max_volume_ratio: float = Field(default=1.35, ge=0.5, le=3.0)
    long_range_position: float = Field(default=0.65, ge=0.5, le=0.95)
    short_range_position: float = Field(default=0.35, ge=0.05, le=0.5)
    max_atr_pct: float = Field(default=0.018, gt=0.002, le=0.10)

    @property
    def resolved_candles_connector(self) -> str:
        return self.candles_connector or self.connector_name

    @property
    def resolved_candles_pair(self) -> str:
        return self.candles_trading_pair or self.trading_pair


class ScanerPremoveV1Controller(_DirectionalTradingControllerBase):
    """Pre-breakout compression + trend alignment controller."""

    def __init__(self, config: AScanerPremoveV1ControllerConfig, *args, **kwargs):
        self.config = config
        self.max_records = max(100, config.range_length + config.ema_slow + 30)
        super().__init__(config, *args, **kwargs)

    async def update_processed_data(self):
        df = self.market_data_provider.get_candles_df(
            connector_name=self.config.resolved_candles_connector,
            trading_pair=self.config.resolved_candles_pair,
            interval=self.config.interval,
            max_records=self.max_records,
        )
        if df.empty or len(df) < self.max_records // 2:
            self.processed_data["signal"] = 0
            self.processed_data["features"] = df
            return

        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        ema_fast = close.ewm(span=self.config.ema_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.config.ema_slow, adjust=False).mean()

        prev_close = close.shift(1)
        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.rolling(14).mean()

        range_high = df["high"].rolling(self.config.range_length).max().shift(1)
        range_low = df["low"].rolling(self.config.range_length).min().shift(1)
        range_width = (range_high - range_low) / close
        range_position = (close - range_low) / (range_high - range_low).replace(0, pd.NA)
        volume_ratio = volume / volume.rolling(self.config.range_length).mean()
        atr_pct = atr / close

        compressed = (
            (range_width <= self.config.max_range_pct)
            & (volume_ratio <= self.config.max_volume_ratio)
            & (atr_pct <= self.config.max_atr_pct)
        )
        long_condition = (
            compressed
            & (ema_fast > ema_slow)
            & (range_position >= self.config.long_range_position)
        )
        short_condition = (
            compressed
            & (ema_fast < ema_slow)
            & (range_position <= self.config.short_range_position)
        )

        df["scaner_range_width_pct"] = range_width * 100
        df["scaner_range_position"] = range_position
        df["scaner_volume_ratio"] = volume_ratio
        df["scaner_atr_pct"] = atr_pct * 100
        df["scaner_ema_fast"] = ema_fast
        df["scaner_ema_slow"] = ema_slow
        df["signal"] = 0
        df.loc[long_condition, "signal"] = 1
        df.loc[short_condition, "signal"] = -1

        self.processed_data["signal"] = int(df["signal"].iloc[-1])
        self.processed_data["features"] = df

    def get_candles_config(self) -> List[CandlesConfig]:
        return [
            CandlesConfig(
                connector=self.config.resolved_candles_connector,
                trading_pair=self.config.resolved_candles_pair,
                interval=self.config.interval,
                max_records=self.max_records,
            )
        ]
