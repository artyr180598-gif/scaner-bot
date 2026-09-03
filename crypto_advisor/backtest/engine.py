"""
Бэктестер (честный, walk-forward).

Идея — как в Freqtrade «backtesting»: на истории свечей модель повторно
строит сигналы и проверяет, что было бы, если бы мы вошли по каждому.
Мы НЕ подгоняем «уверенность» под прибыль — наоборот, считаем winrate
по корзинам уверенности, чтобы честно показать связь (или её отсутствие)
между confidence и исходом.

Результат — BacktestResult с winrate / profit factor / PnL по корзинам.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from ..core.domain.query import UserRequest
from ..core.domain.signal import Direction
from ..analysis.planner import Planner
from ..strategy import get_strategy

log = logging.getLogger(__name__)

WARMUP = 60  # сколько первых баров не оцениваем
LOOKAHEAD = 60  # сколько баров вперёд ждём TP1/SL


@dataclass(slots=True)
class Trade:
    symbol: str
    direction: Direction
    confidence: float
    entry: float
    outcome: str  # "tp1" | "sl" | "timeout"
    pnl_pct: float


@dataclass(slots=True)
class Bucket:
    label: str
    n: int = 0
    winrate: float = 0.0
    profit_factor: float = 0.0
    pnl_pct: float = 0.0


@dataclass(slots=True)
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    buckets: List[Bucket] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    def summary(self) -> str:
        lines = [f"Сделок: {self.total_trades}"]
        by_conf: Dict[str, list] = {}
        for t in self.trades:
            label = f"{min(99, int(t.confidence // 20 * 20))}-{min(100, int(t.confidence // 20 * 20) + 19)}%"
            by_conf.setdefault(label, []).append(t)
        for label in sorted(by_conf):
            trs = by_conf[label]
            wins = sum(1 for t in trs if t.outcome == "tp1")
            wr = wins / len(trs) * 100 if trs else 0
            pnl = sum(t.pnl_pct for t in trs)
            lines.append(f"  {label}: n={len(trs)} winrate={wr:.0f}% pnl={pnl:+.2f}%")
        return "\n".join(lines)


class BacktestEngine:
    """Walk-forward бэктест стратегии на свечах."""

    def __init__(self, strategy=None, planner: Optional[Planner] = None) -> None:
        self.strategy = strategy or get_strategy("momentum_trend")
        self.planner = planner or Planner()

    def run(self, candles_by_symbol: Dict[str, pd.DataFrame]) -> BacktestResult:
        """Прогоняет бэктест по предоставленным свечам (DataFrame с OHLCV)."""
        trades: List[Trade] = []
        for symbol, df in candles_by_symbol.items():
            trades.extend(self._run_symbol(symbol, df))
        result = BacktestResult(trades=trades)
        result.buckets = self._bucketize(trades)
        return result

    def _run_symbol(self, symbol: str, df: pd.DataFrame) -> List[Trade]:
        # Индикаторы считаем один раз на всей истории: все они причинные
        # (ewm/rolling без look-ahead), поэтому значение в баре i совпадает
        # с тем, что дала бы выборка по i.
        enriched = self.strategy.populate_indicators(df)
        from ..indicators import ALL_GROUPS

        trades: List[Trade] = []
        n = len(df)
        for i in range(WARMUP, n - LOOKAHEAD):
            scores: Dict[str, float] = {}
            for g in ALL_GROUPS:
                col = f"{g}_signal_raw"
                if col in enriched.columns:
                    v = enriched[col].iloc[i]
                    scores[g] = float(v) if not pd.isna(v) else 0.0
                else:
                    scores[g] = 0.0
            direction = self.strategy.decide_direction(scores)
            if direction == Direction.NEUTRAL:
                continue
            conf = self.strategy.confidence(scores, direction)
            if conf < 40:  # минимальный порог для участия в статистике
                continue

            # Для плана берём окно до i (без look-ahead).
            window = enriched.iloc[: i + 1].reset_index(drop=True)
            entry = float(window["close"].iloc[-1])
            plan = self.planner.build(window, direction, entry)
            tp1 = plan.take_profits[0].price
            sl = plan.stop_loss.price

            outcome, pnl = self._simulate(df, i + 1, entry, tp1, sl, direction)
            trades.append(Trade(
                symbol=symbol, direction=direction, confidence=conf,
                entry=entry, outcome=outcome, pnl_pct=round(pnl, 3),
            ))
        return trades

    @staticmethod
    def _simulate(df: pd.DataFrame, start: int, entry: float,
                  tp1: float, sl: float, direction: Direction) -> tuple[str, float]:
        """Идёт вперёд и смотрит, что сработало раньше: TP1 или SL."""
        end = min(len(df), start + LOOKAHEAD)
        for j in range(start, end):
            hi = float(df["high"].iloc[j])
            lo = float(df["low"].iloc[j])
            if direction == Direction.LONG:
                if lo <= sl:
                    return "sl", (sl - entry) / entry * 100
                if hi >= tp1:
                    return "tp1", (tp1 - entry) / entry * 100
            else:
                if hi >= sl:
                    return "sl", (entry - sl) / entry * 100
                if lo <= tp1:
                    return "tp1", (entry - tp1) / entry * 100
        return "timeout", 0.0

    @staticmethod
    def _bucketize(trades: List[Trade]) -> List[Bucket]:
        by_conf: Dict[str, List[Trade]] = {}
        for t in trades:
            lo = min(99, int(t.confidence // 20 * 20))
            hi = lo + 19
            by_conf.setdefault(f"{lo}-{hi}", []).append(t)

        buckets: List[Bucket] = []
        for label in sorted(by_conf):
            trs = by_conf[label]
            wins = sum(1 for t in trs if t.outcome == "tp1")
            pnl = sum(t.pnl_pct for t in trs)
            gross_win = sum(t.pnl_pct for t in trs if t.pnl_pct > 0)
            gross_loss = abs(sum(t.pnl_pct for t in trs if t.pnl_pct < 0))
            pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
            buckets.append(Bucket(
                label=label, n=len(trs),
                winrate=round(wins / len(trs) * 100, 1) if trs else 0.0,
                profit_factor=round(pf, 2) if pf != float("inf") else 99.0,
                pnl_pct=round(pnl, 2),
            ))
        return buckets


async def run_backtest_synthetic(symbols=None, timeframe: str = "1h", limit: int = 400) -> BacktestResult:
    """Бэктест на синтетических данных (офлайн-демонстрация)."""
    from ..data.exchange import get_synthetic

    gw = get_synthetic("synthetic")
    symbols = symbols or gw.SYMBOLS[:8]
    data: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        candles = await gw.fetch_candles("synthetic", sym, timeframe, limit=limit)
        if candles is not None:
            data[sym] = candles.df
    engine = BacktestEngine()
    return engine.run(data)
