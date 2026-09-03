"""
Фильтры — отсекают мусор и активы, которые уже улетели.

Уроки BRAIN.md:
- п.0(а): не предлагать активы, которые уже дали сильный рост/падение.
- Анти-мусор: ликвидность, ширина книги, спред.
- Анти-гонка: «актив уже вырос на X% за N баров» — пропускаем.

Каждый фильтр возвращает FilterResult(reason='', passed=True/False).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ...config.settings import Settings
from ...core.domain.candle import Candles
from ...core.domain.market import TickerMeta

log = logging.getLogger(__name__)


@dataclass(slots=True)
class FilterResult:
    passed: bool
    reason: str = ""
    detail: str = ""


class Filters:
    """Набор жёстких фильтров перед построением сигнала."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def check_ticker(self, meta: TickerMeta) -> FilterResult:
        """Анти-мусор фильтр по метаданным тикера."""
        if meta.quote_volume_24h < self.s.min_volume_usd_24h:
            return FilterResult(
                False,
                "low_liquidity",
                f"24h vol ${meta.quote_volume_24h:,.0f} < ${self.s.min_volume_usd_24h:,.0f}",
            )
        if meta.last_price <= 0:
            return FilterResult(False, "bad_price", "last price <= 0")
        if meta.spread_pct > 0.5:
            return FilterResult(
                False, "wide_spread", f"spread {meta.spread_pct:.2f}% > 0.5%"
            )
        return FilterResult(True)

    def check_candles(self, candles: Candles) -> FilterResult:
        """Полнота/свежесть данных."""
        if len(candles) < 50:
            return FilterResult(
                False,
                "not_enough_history",
                f"only {len(candles)} candles (need >= 50)",
            )
        if not candles.is_complete:
            age = candles.last_closed_age_seconds
            return FilterResult(
                False,
                "stale_data",
                f"last candle not complete (age {age}s, period {candles.timeframe})",
            )
        return FilterResult(True)

    def check_volatility(self, df: pd.DataFrame) -> FilterResult:
        """ATR% в допустимом диапазоне."""
        if "atr_pct" not in df.columns:
            return FilterResult(True)  # нет данных — пропускаем мягко
        last_atr = float(df["atr_pct"].iloc[-1])
        if last_atr < self.s.min_atr_pct:
            return FilterResult(
                False, "too_calm", f"ATR% {last_atr:.2f} < {self.s.min_atr_pct}"
            )
        if last_atr > self.s.max_atr_pct:
            return FilterResult(
                False, "too_volatile", f"ATR% {last_atr:.2f} > {self.s.max_atr_pct}"
            )
        return FilterResult(True)

    def check_anti_chase(self, df: pd.DataFrame) -> FilterResult:
        """
        Анти-«погоня за движением».

        Урок BRAIN.md п.0(а): если актив уже дал сильный рост/падение
        за последние N баров — пропускаем, мы ищем ЗАРОЖДЕНИЕ импульса.
        """
        if len(df) < self.s.anti_chase_window_bars + 1:
            return FilterResult(True)
        window = df["close"].iloc[-self.s.anti_chase_window_bars - 1:]
        if window.empty:
            return FilterResult(True)
        change_pct = (window.iloc[-1] / window.iloc[0] - 1) * 100
        if abs(change_pct) > self.s.anti_chase_max_pct:
            side = "up" if change_pct > 0 else "down"
            return FilterResult(
                False,
                "anti_chase",
                f"already moved {change_pct:+.2f}% in last {self.s.anti_chase_window_bars} bars ({side})",
            )
        return FilterResult(True)

    def check_min_confidence(
        self, signal_confidence: float, data_confidence: float
    ) -> FilterResult:
        """Жёсткий порог 75% по ТЗ (на signal confidence)."""
        if signal_confidence < self.s.min_confidence:
            return FilterResult(
                False,
                "low_confidence",
                f"signal confidence {signal_confidence:.1f}% < {self.s.min_confidence:.1f}%",
            )
        if data_confidence < 60.0:
            return FilterResult(
                False,
                "low_data_quality",
                f"data confidence {data_confidence:.1f}% < 60%",
            )
        return FilterResult(True)

    def all_pre_signal(
        self, meta: TickerMeta, candles: Candles, df: pd.DataFrame
    ) -> FilterResult:
        """Запускает все pre-signal фильтры по цепочке."""
        for fn, args in [
            (self.check_ticker, (meta,)),
            (self.check_candles, (candles,)),
            (self.check_volatility, (df,)),
            (self.check_anti_chase, (df,)),
        ]:
            res = fn(*args)
            if not res.passed:
                return res
        return FilterResult(True)
