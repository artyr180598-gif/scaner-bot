"""
Планировщик сделки.

Вход — текущая зона (ATR-буфер вокруг цены).
Цели — 1R/2R/3R от стопа.
Стоп — за последним локальным экстремумом + ATR-буфер.
Плечо — выбирается из risk/reward + волатильности.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from ...core.domain.signal import (
    Direction,
    SignalPlan,
    StopLoss,
    TakeProfit,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Planner:
    """Строит SignalPlan по индикаторному df + направлению."""

    # Цели — кратные R от стопа.
    tp_multiples: Tuple[float, ...] = (1.0, 2.0, 3.0)
    # Доля депозита под риск.
    risk_per_trade_pct: float = 1.0
    # Максимальное плечо (для фьючерсов).
    max_leverage: float = 5.0

    def build(
        self,
        df: pd.DataFrame,
        direction: Direction,
        last_price: float,
    ) -> SignalPlan:
        atr_pct = float(df.get("atr_pct", pd.Series([1.0])).iloc[-1]) if "atr_pct" in df.columns else 1.0
        atr_price = last_price * atr_pct / 100.0

        # Зона входа: ±0.25*ATR вокруг текущей цены.
        entry_low = last_price - 0.25 * atr_price
        entry_high = last_price + 0.25 * atr_price
        entry_mid = last_price

        # Стоп: за последним локальным экстремумом + ATR.
        stop_price = self._stop_price(df, direction, last_price, atr_price)
        risk = abs(entry_mid - stop_price) or atr_price

        # Цели: TP1 = 1R, TP2 = 2R, TP3 = 3R.
        if direction == Direction.LONG:
            tps = [
                TakeProfit(level=i + 1, price=entry_mid + mult * risk, pct_from_entry=mult * risk / entry_mid * 100)
                for i, mult in enumerate(self.tp_multiples)
            ]
        else:
            tps = [
                TakeProfit(level=i + 1, price=entry_mid - mult * risk, pct_from_entry=-mult * risk / entry_mid * 100)
                for i, mult in enumerate(self.tp_multiples)
            ]

        rr = (tps[0].price - entry_mid) / risk if direction == Direction.LONG else (entry_mid - tps[0].price) / risk

        # Плечо: грубая формула — RR * (risk_pct / max_loss_pct).
        # Для RR=2 и 1% риска: плечо ≈ 2; для RR=3 → 3.
        leverage = min(self.max_leverage, max(1.0, rr * (self.risk_per_trade_pct / 1.0)))

        stop_pct = (stop_price - entry_mid) / entry_mid * 100 * (1 if direction == Direction.LONG else -1)
        sl = StopLoss(price=stop_price, pct_from_entry=stop_pct,
                      rationale=self._stop_rationale(df, direction, stop_price))

        return SignalPlan(
            entry_zone=(entry_low, entry_high),
            entry_mid=entry_mid,
            take_profits=tps,
            stop_loss=sl,
            risk_reward=round(rr, 2),
            leverage_suggestion=round(leverage, 2),
        )

    # ── Helpers ───────────────────────────────────────────────
    @staticmethod
    def _stop_price(df: pd.DataFrame, direction: Direction, last: float, atr: float) -> float:
        """Стоп за последним локальным low/high + ATR-буфер."""
        lookback = 10
        if len(df) < lookback:
            return last - atr * 1.5 if direction == Direction.LONG else last + atr * 1.5
        recent = df.iloc[-lookback:]
        if direction == Direction.LONG:
            swing = float(recent["low"].min())
            return swing - 0.5 * atr
        else:
            swing = float(recent["high"].max())
            return swing + 0.5 * atr

    @staticmethod
    def _stop_rationale(df: pd.DataFrame, direction: Direction, stop: float) -> str:
        lookback = 10
        if len(df) < lookback:
            return f"Стоп {lookback} баров + ATR буфер"
        recent = df.iloc[-lookback:]
        if direction == Direction.LONG:
            lvl = float(recent["low"].min())
            return f"За локальным минимумом {lvl:.4f}"
        lvl = float(recent["high"].max())
        return f"За локальным максимумом {lvl:.4f}"
