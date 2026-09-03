"""
Бэктестер.

Идём по историческим свечам, на каждом «сейчас» запускаем полный пайплайн
сигналов (indicators → scoring → filters → plan). Если сигнал прошёл 75%
порог — симулируем сделку (вход по плану, выход по TP/SL/таймауту) и пишем
в журнал. Считаем честные метрики.

Уроки BRAIN.md (не повторяем):
- п.0b: «уверенность прогноза» НЕ равно вероятности profit. Бэктест это
  докажет или опровергнет.
- п.0c: гейт anti_chase уже встроен в filters, проверяется здесь тоже.
- п.9: pandas-3 datetime[us] — нормализуем таймстемпы.
- п.10: движок кормится ВСЕМИ барами, а не только «прошедшими порог».
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from ..analysis.filters import Filters
from ..analysis.plan import Planner
from ..analysis.scoring import Scorer
from ..config.settings import Settings, get_settings
from ..core.domain.candle import Candle, Candles
from ..core.domain.market import TickerMeta
from ..core.domain.signal import (
    Confidences, Direction, Signal, SignalPlan, SignalStatus,
    StopLoss, TakeProfit,
)
from ..core.domain.ticker import Ticker
from .data_loader import fetch_okx_candles

log = logging.getLogger(__name__)


@dataclass
class Trade:
    """Одна симулированная сделка из бэктеста."""

    symbol: str
    direction: Direction
    entry_time: int  # ms
    entry_price: float
    exit_time: int
    exit_price: float
    exit_reason: str  # "tp1" | "tp2" | "tp3" | "sl" | "timeout"
    pnl_pct: float  # % от входа
    confidence: float  # signal confidence на момент входа
    bars_held: int

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "entry_time": self.entry_time,
            "entry_price": round(self.entry_price, 4),
            "exit_time": self.exit_time,
            "exit_price": round(self.exit_price, 4),
            "exit_reason": self.exit_reason,
            "pnl_pct": round(self.pnl_pct, 3),
            "confidence": round(self.confidence, 1),
            "bars_held": self.bars_held,
        }


@dataclass
class BacktestResult:
    """Итог бэктеста."""

    symbol: str
    timeframe: str
    bars_total: int
    signals_generated: int
    trades: List[Trade] = field(default_factory=list)
    start_ts: int = 0
    end_ts: int = 0
    duration_seconds: float = 0.0

    # ── Метрики ─────────────────────────────────────────────
    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def n_wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_pct > 0)

    @property
    def n_losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl_pct <= 0)

    @property
    def winrate(self) -> float:
        return self.n_wins / max(1, self.n_trades)

    @property
    def total_pnl_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl_pct > 0]
        return float(np.mean(wins)) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl_pct <= 0]
        return float(np.mean(losses)) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        """PF = (sum wins) / |sum losses|. > 1 = прибыльно."""
        gross_profit = sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)
        gross_loss = abs(sum(t.pnl_pct for t in self.trades if t.pnl_pct <= 0))
        if gross_loss < 1e-9:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def max_drawdown_pct(self) -> float:
        """Max drawdown по equity-curve."""
        if not self.trades:
            return 0.0
        equity = np.cumsum([t.pnl_pct for t in self.trades])
        peak = np.maximum.accumulate(np.concatenate([[0], equity]))
        dd = peak[1:] - equity
        return float(dd.max()) if len(dd) > 0 else 0.0

    def winrate_by_confidence_bucket(self) -> Dict[str, dict]:
        """Сгруппировать сделки по бакетам confidence. Главный тест гипотезы
        «уверенность ≥ 75% = прибыль» (BRAIN.md п.0b)."""
        buckets = {
            "<60%": [],
            "60-75%": [],
            "≥75%": [],
        }
        for t in self.trades:
            if t.confidence < 60:
                buckets["<60%"].append(t)
            elif t.confidence < 75:
                buckets["60-75%"].append(t)
            else:
                buckets["≥75%"].append(t)
        out: Dict[str, dict] = {}
        for name, trades in buckets.items():
            if not trades:
                out[name] = {"n": 0, "winrate": 0.0, "pf": 0.0, "total_pnl": 0.0}
                continue
            wins = [t for t in trades if t.pnl_pct > 0]
            losses = [t for t in trades if t.pnl_pct <= 0]
            gp = sum(t.pnl_pct for t in wins)
            gl = abs(sum(t.pnl_pct for t in losses))
            pf = gp / gl if gl > 1e-9 else float("inf")
            out[name] = {
                "n": len(trades),
                "winrate": len(wins) / len(trades),
                "pf": pf,
                "total_pnl": sum(t.pnl_pct for t in trades),
                "avg_confidence": float(np.mean([t.confidence for t in trades])),
            }
        return out

    def report(self) -> str:
        """Человекочитаемый отчёт."""
        lines = [
            f"=== Backtest: {self.symbol} {self.timeframe} ===",
            f"Период: {pd.Timestamp(self.start_ts, unit='ms')} → "
            f"{pd.Timestamp(self.end_ts, unit='ms')}",
            f"Баров: {self.bars_total}  Сигналов: {self.signals_generated}  Сделок: {self.n_trades}",
            "",
            f"Winrate: {self.winrate:.1%}  ({self.n_wins}W / {self.n_losses}L)",
            f"Total PnL: {self.total_pnl_pct:+.2f}%",
            f"Avg win: {self.avg_win:+.2f}%  Avg loss: {self.avg_loss:+.2f}%",
            f"Profit factor: {self.profit_factor:.2f}",
            f"Max drawdown: {self.max_drawdown_pct:.2f}%",
            "",
            "── По бакетам confidence ──",
        ]
        buckets = self.winrate_by_confidence_bucket()
        for name, b in buckets.items():
            pf = f"{b['pf']:.2f}" if b['pf'] != float('inf') else "inf"
            lines.append(
                f"  {name:<8} n={b['n']:>3}  winrate={b['winrate']:>6.1%}  "
                f"PF={pf:>6}  PnL={b['total_pnl']:+.2f}%"
            )
        if not self.trades:
            lines.append("(сделок нет — стратегия молчит)")
        return "\n".join(lines)


class BacktestEngine:
    """
    Бэктестер на исторических данных.

    Параметры:
        symbol, timeframe, days: какие данные тянуть.
        max_bars: ограничение (для скорости).
        exit_timeout_bars: сколько баров держать позицию, если не сработал TP/SL.
        signal_fn: опциональный кастомный генератор сигналов (для тестов).
    """

    def __init__(
        self,
        symbol: str = "BTC-USDT",
        timeframe: str = "1H",
        days: int = 30,
        max_bars: int = 720,
        exit_timeout_bars: int = 24,
        settings: Optional[Settings] = None,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.days = days
        self.max_bars = max_bars
        self.exit_timeout_bars = exit_timeout_bars
        import dataclasses
        base_s = settings or get_settings()
        # Прогоняем с пониженным top_n_symbols (1 монета в бэктесте).
        self.s = dataclasses.replace(base_s, top_n_symbols=1) if base_s.top_n_symbols != 1 else base_s
        self.scorer = Scorer(self.s)
        self.filters = Filters(self.s)
        self.planner = Planner()

    def run(
        self,
        candles: Optional[List[Candle]] = None,
        progress: bool = False,
    ) -> BacktestResult:
        """
        Прогон бэктеста.

        Если `candles` не переданы — тянет OKX за `days * 24h / TF`.
        """
        if candles is None:
            limit = min(self.max_bars, self.days * 24)  # для 1H
            log.info("[backtest] fetching %d candles of %s %s", limit, self.symbol, self.timeframe)
            candles = fetch_okx_candles(self.symbol, self.timeframe, limit=limit)
        if not candles or len(candles) < 100:
            log.warning("[backtest] not enough candles: %d", len(candles))
            return BacktestResult(
                symbol=self.symbol, timeframe=self.timeframe, bars_total=len(candles),
                signals_generated=0, start_ts=0, end_ts=0,
            )

        result = BacktestResult(
            symbol=self.symbol, timeframe=self.timeframe,
            bars_total=len(candles),
            signals_generated=0,
            start_ts=candles[0].timestamp,
            end_ts=candles[-1].timestamp,
        )
        t0 = time.time()

        # Минимум 50 баров для индикаторов, далее скользящее окно.
        warmup = 50
        # Тянем только последние N баров.
        n = min(self.max_bars, len(candles))
        window = candles[-n:]

        # Предвычисляем индикаторы на полном окне один раз,
        # потом берём «срез до i» — это сильно ускоряет бэктест.
        full_candles = Candles(
            exchange="okx", symbol=self.symbol, timeframe=self.timeframe,
            _data=window,
        )
        full_df = full_candles.df
        log.info("[backtest] enriching %d bars with indicators (one pass)…", n)
        full_enriched = self.scorer.enrich(full_df)
        # Заполняем NaN, чтобы group_signal не падал.
        full_enriched = full_enriched.ffill().fillna(0)

        for i in range(warmup, n):
            current = window[i]
            # Берём срез enriched df до i (включительно).
            df_slice = full_enriched.iloc[:i + 1]
            signal = self._try_signal_from_enriched(df_slice, current)
            if signal is None:
                continue
            result.signals_generated += 1

            trade = self._simulate_trade(signal, window, i)
            if trade is not None:
                result.trades.append(trade)
            if progress and i % 100 == 0:
                log.info("[backtest] %d/%d bars, %d signals, %d trades",
                         i, n, result.signals_generated, len(result.trades))

        result.duration_seconds = time.time() - t0
        return result

    # ── Internals ─────────────────────────────────────────────
    def _try_signal(
        self, history: List[Candle], current: Candle
    ) -> Optional[Signal]:
        """Попробовать сгенерировать сигнал на текущем баре (legacy — без кеша)."""
        try:
            candles = Candles(
                exchange="okx",
                symbol=self.symbol,
                timeframe=self.timeframe,
                _data=history,
            )
            df = candles.df
            if df.empty or len(df) < 50:
                return None
            return self._try_signal_from_enriched(self.scorer.enrich(df), current)
        except Exception as exc:  # noqa: BLE001
            log.debug("signal gen failed: %s", exc)
            return None

    def _try_signal_from_enriched(
        self, df: pd.DataFrame, current: Candle
    ) -> Optional[Signal]:
        """Генерирует сигнал по уже обогащённому df (быстрее)."""
        try:
            if df.empty or len(df) < 50:
                return None
            df = df.ffill().fillna(0)
            group_scores = self.scorer.score_groups(df)
            direction = self.scorer.decide_direction(group_scores)
            if direction == Direction.NEUTRAL:
                return None
            conf = self.scorer.build_confidences(df, group_scores, direction)
            if conf.signal < self.s.min_confidence or conf.data < 60:
                return None
            plan = self.planner.build(df, direction, current.close)
            return Signal(
                symbol=self.symbol,
                exchange="okx",
                direction=direction,
                last_price=current.close,
                timeframe_base=self.timeframe,
                timeframes_used=[self.timeframe],
                confidences=conf,
                plan=plan,
                entry_logic="backtest",
                signal_id=f"bt-{current.timestamp}",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("signal gen failed: %s", exc)
            return None

    def _simulate_trade(
        self, signal: Signal, window: List[Candle], entry_idx: int
    ) -> Optional[Trade]:
        """
        Симулируем вход и выход.

        Вход = середина зоны. Проверяем TP1/TP2/TP3/SL на каждом следующем баре.
        Таймаут = exit_timeout_bars.
        """
        direction = signal.direction
        entry_price = signal.plan.entry_mid
        if entry_idx + 1 >= len(window):
            return None
        # Проверяем, что вход «достижим» на следующем баре (его high/low покрывает зону).
        next_bar = window[entry_idx + 1]
        if direction == Direction.LONG:
            # Long: нужно, чтобы low следующего бара был ниже entry → можем купить.
            if next_bar.low > entry_price:
                return None  # не дошли до нашей зоны
        else:
            # Short: нужно, чтобы high был выше entry → можем продать.
            if next_bar.high < entry_price:
                return None

        sl = signal.plan.stop_loss.price
        tps = signal.plan.take_profits
        # Идём по барам и ищем первый TP/SL/таймаут.
        max_hold = min(self.exit_timeout_bars, len(window) - entry_idx - 1)
        for j in range(1, max_hold + 1):
            bar = window[entry_idx + j]
            if direction == Direction.LONG:
                # Сначала проверяем SL (худшее), потом TP.
                if bar.low <= sl:
                    pnl = (sl / entry_price - 1) * 100
                    return Trade(
                        symbol=self.symbol, direction=direction,
                        entry_time=window[entry_idx].timestamp,
                        entry_price=entry_price,
                        exit_time=bar.timestamp, exit_price=sl,
                        exit_reason="sl", pnl_pct=pnl,
                        confidence=signal.confidences.signal, bars_held=j,
                    )
                for tp in tps:
                    if bar.high >= tp.price:
                        pnl = (tp.price / entry_price - 1) * 100
                        return Trade(
                            symbol=self.symbol, direction=direction,
                            entry_time=window[entry_idx].timestamp,
                            entry_price=entry_price,
                            exit_time=bar.timestamp, exit_price=tp.price,
                            exit_reason=f"tp{tp.level}", pnl_pct=pnl,
                            confidence=signal.confidences.signal, bars_held=j,
                        )
            else:  # SHORT
                if bar.high >= sl:
                    pnl = (entry_price / sl - 1) * 100
                    return Trade(
                        symbol=self.symbol, direction=direction,
                        entry_time=window[entry_idx].timestamp,
                        entry_price=entry_price,
                        exit_time=bar.timestamp, exit_price=sl,
                        exit_reason="sl", pnl_pct=pnl,
                        confidence=signal.confidences.signal, bars_held=j,
                    )
                for tp in tps:
                    if bar.low <= tp.price:
                        pnl = (entry_price / tp.price - 1) * 100
                        return Trade(
                            symbol=self.symbol, direction=direction,
                            entry_time=window[entry_idx].timestamp,
                            entry_price=entry_price,
                            exit_time=bar.timestamp, exit_price=tp.price,
                            exit_reason=f"tp{tp.level}", pnl_pct=pnl,
                            confidence=signal.confidences.signal, bars_held=j,
                        )
        # Таймаут: закрываем по цене последнего доступного бара.
        last_bar = window[entry_idx + max_hold]
        exit_price = last_bar.close
        pnl = (exit_price / entry_price - 1) * 100 * (1 if direction == Direction.LONG else -1)
        return Trade(
            symbol=self.symbol, direction=direction,
            entry_time=window[entry_idx].timestamp,
            entry_price=entry_price,
            exit_time=last_bar.timestamp, exit_price=exit_price,
            exit_reason="timeout", pnl_pct=pnl,
            confidence=signal.confidences.signal, bars_held=max_hold,
        )
