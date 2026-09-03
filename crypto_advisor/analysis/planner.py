"""
Планировщик сделки.

Вход — текущая зона (ATR-буфер вокруг цены).
Цели — 1R/2R/3R от стопа.
Стоп — за последним локальным экстремумом + ATR-буфер.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import pandas as pd

from ..core.domain.signal import Direction, SignalPlan, StopLoss, TakeProfit


@dataclass(slots=True)
class Planner:
    """Строит SignalPlan по индикаторному df + направлению."""

    tp_multiples: Tuple[float, ...] = (1.0, 2.0, 3.0)
    risk_per_trade_pct: float = 1.0
    max_leverage: float = 5.0

    def build(self, df: pd.DataFrame, direction: Direction, last_price: float) -> SignalPlan:
        atr_pct = float(df.get("atr_pct", pd.Series([1.0])).iloc[-1]) if "atr_pct" in df.columns else 1.0
        atr_price = last_price * max(atr_pct, 0.05) / 100.0

        entry_low = last_price - 0.25 * atr_price
        entry_high = last_price + 0.25 * atr_price
        entry_mid = last_price

        # Риск = расстояние до стопа, но ограничиваем его ВОЛАТИЛЬНОСТЬЮ,
        # чтобы на любых данных (даже с далёким свингом) план был осмысленным:
        # стоп не дальше 2.5*ATR и не ближе 1*ATR от цены.
        raw_stop = self._stop_price(df, direction, last_price, atr_price)
        raw_dist = abs(raw_stop - entry_mid)
        dist = min(max(raw_dist, 1.0 * atr_price), 2.5 * atr_price)
        if direction == Direction.LONG:
            stop_price = entry_mid - dist
        else:
            stop_price = entry_mid + dist
        risk = dist or atr_price

        if direction == Direction.LONG:
            tps = [
                TakeProfit(i + 1, entry_mid + mult * risk, mult * risk / entry_mid * 100)
                for i, mult in enumerate(self.tp_multiples)
            ]
        else:
            tps = [
                TakeProfit(i + 1, entry_mid - mult * risk, -mult * risk / entry_mid * 100)
                for i, mult in enumerate(self.tp_multiples)
            ]

        rr = ((tps[0].price - entry_mid) / risk if direction == Direction.LONG
              else (entry_mid - tps[0].price) / risk)
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

    @staticmethod
    def _stop_price(df: pd.DataFrame, direction: Direction, last: float, atr: float) -> float:
        lookback = 10
        if len(df) < lookback:
            return last - atr * 1.5 if direction == Direction.LONG else last + atr * 1.5
        recent = df.iloc[-lookback:]
        if direction == Direction.LONG:
            return float(recent["low"].min()) - 0.5 * atr
        return float(recent["high"].max()) + 0.5 * atr

    @staticmethod
    def _stop_rationale(df: pd.DataFrame, direction: Direction, stop: float) -> str:
        lookback = 10
        if len(df) < lookback:
            return f"Стоп {lookback} баров + ATR буфер"
        recent = df.iloc[-lookback:]
        if direction == Direction.LONG:
            return f"За локальным минимумом {float(recent['low'].min()):.4f}"
        return f"За локальным максимумом {float(recent['high'].max()):.4f}"
