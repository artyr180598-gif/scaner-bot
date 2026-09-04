from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import numpy as np

from cryptopilot.indicators import atr, dmi, ema, rsi
from cryptopilot.models import Candle


@dataclass(frozen=True, slots=True)
class TradeRecord:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    side: str
    confidence: int
    required_confidence: int
    score: float
    regime: str
    entry: float
    exit: float
    stop: float
    target: float
    result_r: float
    outcome: str

    def to_dict(self) -> dict:
        result = asdict(self)
        result["entry_time"] = self.entry_time.isoformat()
        result["exit_time"] = self.exit_time.isoformat()
        return result


@dataclass(frozen=True, slots=True)
class SymbolResearchResult:
    symbol: str
    first_bar: datetime
    last_bar: datetime
    bars: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_r: float
    expectancy_r: float
    profit_factor: float
    max_drawdown_r: float
    max_drawdown_pct: float
    ending_equity: float
    return_pct: float
    yearly_r: dict[str, float]
    confidence_bins: dict[str, dict[str, float]]
    records: tuple[TradeRecord, ...]

    def to_dict(self, include_records: bool = True) -> dict:
        result = asdict(self)
        result["first_bar"] = self.first_bar.isoformat()
        result["last_bar"] = self.last_bar.isoformat()
        if include_records:
            result["records"] = [record.to_dict() for record in self.records]
        else:
            result.pop("records", None)
        return result


@dataclass(frozen=True, slots=True)
class FeatureArrays:
    score: np.ndarray
    ema20: np.ndarray
    ema50: np.ndarray
    ema200: np.ndarray
    rsi14: np.ndarray
    atr14: np.ndarray
    atr_pct: np.ndarray
    adx14: np.ndarray
    plus_di14: np.ndarray
    minus_di14: np.ndarray
    dmi_spread: np.ndarray
    volume_z: np.ndarray
    efficiency_ratio20: np.ndarray
    ema_gap_atr: np.ndarray
    atr_regime_ratio: np.ndarray
    return_20_pct: np.ndarray


def aggregate_candles(candles: list[Candle], minutes: int) -> list[Candle]:
    if minutes % 15:
        raise ValueError("Research aggregation expects a multiple of 15 minutes")
    period_ms = minutes * 60_000
    expected = minutes // 15
    output: list[Candle] = []
    group: list[Candle] = []
    active_bucket: int | None = None

    def flush() -> None:
        if len(group) != expected:
            return
        if group[-1].open_time_ms - group[0].open_time_ms != (expected - 1) * 900_000:
            return
        output.append(
            Candle(
                open_time_ms=group[0].open_time_ms,
                open=group[0].open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=group[-1].close,
                volume=sum(item.volume for item in group),
                turnover=sum(item.turnover for item in group),
            )
        )

    for candle in candles:
        bucket = candle.open_time_ms // period_ms
        if active_bucket is None:
            active_bucket = bucket
        if bucket != active_bucket:
            flush()
            group = []
            active_bucket = bucket
        group.append(candle)
    flush()
    return output


def feature_arrays(candles: list[Candle]) -> FeatureArrays:
    if len(candles) < 210:
        raise ValueError("At least 210 candles are required")
    close = np.asarray([item.close for item in candles], dtype=float)
    high = np.asarray([item.high for item in candles], dtype=float)
    low = np.asarray([item.low for item in candles], dtype=float)
    volume = np.asarray([item.volume for item in candles], dtype=float)

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    plus_di14, minus_di14, adx14 = dmi(high, low, close, 14)
    macd = ema(close, 12) - ema(close, 26)
    macd_hist = macd - ema(macd, 9)

    slope = np.zeros_like(close)
    slope[5:] = (ema20[5:] / ema20[:-5] - 1) * 100
    return20 = np.zeros_like(close)
    return20[20:] = (close[20:] / close[:-20] - 1) * 100
    volume_z = rolling_zscore(volume, 30)
    efficiency_ratio20 = rolling_efficiency_ratio(close, 20)
    ema_gap_atr = np.divide(
        np.abs(ema20 - ema50), atr14, out=np.zeros_like(close), where=atr14 > 1e-12
    )
    atr_regime_ratio = rolling_median_ratio(atr14, 100)
    breakout_up = np.zeros(len(close), dtype=bool)
    breakout_down = np.zeros(len(close), dtype=bool)
    for index in range(20, len(close)):
        breakout_up[index] = close[index] > np.max(high[index - 20 : index])
        breakout_down[index] = close[index] < np.min(low[index - 20 : index])

    score = np.where(
        (close > ema20) & (ema20 > ema50) & (ema50 > ema200),
        34.0,
        np.where(
            (close < ema20) & (ema20 < ema50) & (ema50 < ema200),
            -34.0,
            np.where(close > ema50, 10.0, -10.0),
        ),
    )
    score += np.clip(slope * 7, -14, 14)
    score += np.where(
        (rsi14 >= 52) & (rsi14 <= 68),
        12,
        np.where(
            (rsi14 >= 32) & (rsi14 <= 48),
            -12,
            np.where(rsi14 > 78, -8, np.where(rsi14 < 22, 8, 0)),
        ),
    )
    score += np.where(macd_hist > 0, 10, -10)
    score += np.where(breakout_up, 10, np.where(breakout_down, -10, 0))
    score += np.where(
        volume_z > 1,
        np.where(return20 > 0, 8, -8),
        0,
    )
    score = np.where(adx14 < 16, score * 0.72, score)
    score = np.clip(score, -100, 100)
    score[:209] = 0
    return FeatureArrays(
        score=score,
        ema20=ema20,
        ema50=ema50,
        ema200=ema200,
        rsi14=rsi14,
        atr14=atr14,
        atr_pct=np.divide(atr14, close, out=np.zeros_like(close), where=close > 0) * 100,
        adx14=adx14,
        plus_di14=plus_di14,
        minus_di14=minus_di14,
        dmi_spread=np.divide(
            plus_di14 - minus_di14,
            plus_di14 + minus_di14,
            out=np.zeros_like(close),
            where=(plus_di14 + minus_di14) > 1e-12,
        )
        * 100,
        volume_z=volume_z,
        efficiency_ratio20=efficiency_ratio20,
        ema_gap_atr=ema_gap_atr,
        atr_regime_ratio=atr_regime_ratio,
        return_20_pct=return20,
    )


def rolling_zscore(values: np.ndarray, period: int) -> np.ndarray:
    result = np.zeros_like(values, dtype=float)
    cumulative = np.concatenate(([0.0], np.cumsum(values)))
    cumulative_sq = np.concatenate(([0.0], np.cumsum(values * values)))
    for index in range(period - 1, len(values)):
        start = index + 1 - period
        mean = (cumulative[index + 1] - cumulative[start]) / period
        variance = (cumulative_sq[index + 1] - cumulative_sq[start]) / period - mean * mean
        deviation = math.sqrt(max(0.0, variance))
        result[index] = (values[index] - mean) / deviation if deviation > 1e-12 else 0
    return result


def rolling_efficiency_ratio(values: np.ndarray, period: int) -> np.ndarray:
    result = np.zeros_like(values, dtype=float)
    movement = np.abs(np.diff(values, prepend=values[0]))
    cumulative = np.concatenate(([0.0], np.cumsum(movement)))
    for index in range(period, len(values)):
        noise = cumulative[index + 1] - cumulative[index + 1 - period]
        direction = abs(values[index] - values[index - period])
        result[index] = direction / noise if noise > 1e-12 else 0.0
    return result


def rolling_median_ratio(values: np.ndarray, period: int) -> np.ndarray:
    result = np.ones_like(values, dtype=float)
    for index in range(period - 1, len(values)):
        median = float(np.median(values[index + 1 - period : index + 1]))
        result[index] = values[index] / median if median > 1e-12 else 1.0
    return result


class MultiTimeframeResearchBacktester:
    """Historical approximation of the production 15m/1h/4h signal engine.

    It preserves the decision score, regime gate, auto-confidence threshold, entry zone,
    stop construction and TP2 target. Historical funding, spread and cross-sectional
    shortlist membership are unavailable in public kline archives and are explicitly not
    simulated.
    """

    def __init__(
        self,
        auto_confidence: int = 84,
        short_confidence: int = 86,
        risk_per_trade_pct: float = 0.5,
        one_way_cost_bps: float = 6.0,
        cooldown_bars: int = 12,
        max_holding_bars: int = 672,
        starting_equity: float = 1000.0,
        min_primary_adx: float = 18.0,
        min_efficiency_ratio: float = 0.14,
        min_ema_gap_atr: float = 0.08,
        max_atr_regime_ratio: float = 2.8,
        relative_strength_filter: bool = True,
        neutral_regime_confidence_penalty: int = 2,
    ) -> None:
        self.auto_confidence = auto_confidence
        self.short_confidence = short_confidence
        self.risk_fraction = risk_per_trade_pct / 100
        self.cost_fraction = one_way_cost_bps / 10_000
        self.cooldown_bars = cooldown_bars
        self.max_holding_bars = max_holding_bars
        self.starting_equity = starting_equity
        self.min_primary_adx = min_primary_adx
        self.min_efficiency_ratio = min_efficiency_ratio
        self.min_ema_gap_atr = min_ema_gap_atr
        self.max_atr_regime_ratio = max_atr_regime_ratio
        self.relative_strength_filter = relative_strength_filter
        self.neutral_regime_confidence_penalty = neutral_regime_confidence_penalty

    def run(
        self, symbol: str, candles_15m: list[Candle], benchmark_15m: list[Candle]
    ) -> SymbolResearchResult:
        if len(candles_15m) < 4000 or len(benchmark_15m) < 4000:
            raise ValueError("Long research needs at least ~42 days of 15m history")
        candles_1h = aggregate_candles(candles_15m, 60)
        candles_4h = aggregate_candles(candles_15m, 240)
        benchmark_4h = aggregate_candles(benchmark_15m, 240)
        f15 = feature_arrays(candles_15m)
        f1h = feature_arrays(candles_1h)
        f4h = feature_arrays(candles_4h)
        f_benchmark = feature_arrays(benchmark_4h)

        close_1h = [item.open_time_ms + 3_600_000 for item in candles_1h]
        close_4h = [item.open_time_ms + 14_400_000 for item in candles_4h]
        close_benchmark = [item.open_time_ms + 14_400_000 for item in benchmark_4h]
        records: list[TradeRecord] = []
        index = 210

        while index < len(candles_15m) - 2:
            decision_time = candles_15m[index].open_time_ms + 900_000
            index_1h = bisect.bisect_right(close_1h, decision_time) - 1
            index_4h = bisect.bisect_right(close_4h, decision_time) - 1
            benchmark_index = bisect.bisect_right(close_benchmark, decision_time) - 1
            if min(index_1h, index_4h, benchmark_index) < 209:
                index += 1
                continue

            scores = (f15.score[index], f1h.score[index_1h], f4h.score[index_4h])
            directions = tuple(int(np.sign(item)) for item in scores)
            higher_aligned = directions[1] == directions[2] != 0
            aligned = len(set(directions)) == 1 and directions[0] != 0
            score = scores[0] * 0.25 + scores[1] * 0.35 + scores[2] * 0.40
            long = score > 0
            benchmark_score = f_benchmark.score[benchmark_index]
            if f_benchmark.adx14[benchmark_index] < 16:
                regime = "RANGE"
            elif benchmark_score >= 25:
                regime = "BULL"
            elif benchmark_score <= -25:
                regime = "BEAR"
            else:
                regime = "TRANSITION"

            if not higher_aligned or abs(score) < 45:
                index += 1
                continue
            if not 0.22 <= f1h.atr_pct[index_1h] <= 7.5:
                index += 1
                continue
            if f1h.adx14[index_1h] < self.min_primary_adx:
                index += 1
                continue
            if f1h.efficiency_ratio20[index_1h] < self.min_efficiency_ratio:
                index += 1
                continue
            if f1h.ema_gap_atr[index_1h] < self.min_ema_gap_atr:
                index += 1
                continue
            if f1h.atr_regime_ratio[index_1h] > self.max_atr_regime_ratio:
                index += 1
                continue
            if long and f1h.dmi_spread[index_1h] < -5:
                index += 1
                continue
            if not long and f1h.dmi_spread[index_1h] > 5:
                index += 1
                continue
            if long and regime == "BEAR" and benchmark_score < -45:
                index += 1
                continue

            if self.relative_strength_filter and symbol != "BTCUSDT":
                relative_edge = (
                    f4h.return_20_pct[index_4h]
                    - f_benchmark.return_20_pct[benchmark_index]
                )
                tolerance = max(1.0, f_benchmark.atr_pct[benchmark_index] * 1.5)
                if (long and relative_edge < -tolerance) or (
                    not long and relative_edge > tolerance
                ):
                    index += 1
                    continue
            if not long and regime == "BULL" and benchmark_score > 45:
                index += 1
                continue

            signal_price = candles_15m[index].close
            execution_atr = f15.atr14[index]
            chase = abs(signal_price - f15.ema20[index]) / max(execution_atr, 1e-12)
            if chase > 2.4:
                index += 1
                continue

            risks = 0
            if f1h.adx14[index_1h] < 22:
                risks += 1
            if (long and f15.rsi14[index] > 70) or (not long and f15.rsi14[index] < 30):
                risks += 1
            if f15.volume_z[index] < -0.5:
                risks += 1
            confidence = 50 + abs(score) * 0.36 + (5 if aligned else 0)
            if (long and regime == "BULL") or (not long and regime == "BEAR"):
                confidence += 3
            confidence += 2 if f1h.adx14[index_1h] >= 25 else 0
            confidence += 2  # Selected symbols are high-liquidity majors.
            confidence -= min(8, risks * 2)
            confidence = int(np.clip(round(confidence), 50, 89))
            required_confidence = self.auto_confidence if long else self.short_confidence
            if regime in {"RANGE", "TRANSITION"}:
                required_confidence += self.neutral_regime_confidence_penalty
            if confidence < required_confidence:
                index += 1
                continue

            recent = candles_15m[index - 17 : index + 1]
            if long:
                entry_low = signal_price - 0.18 * execution_atr
                entry_high = signal_price + 0.05 * execution_atr
                stop = min(min(item.low for item in recent), signal_price - 1.45 * execution_atr)
                distance = signal_price - stop
                target = signal_price + 2 * distance
            else:
                entry_low = signal_price - 0.05 * execution_atr
                entry_high = signal_price + 0.18 * execution_atr
                stop = max(max(item.high for item in recent), signal_price + 1.45 * execution_atr)
                distance = stop - signal_price
                target = signal_price - 2 * distance
            stop_pct = distance / signal_price * 100
            if not 0.25 <= stop_pct <= 5:
                index += 1
                continue

            entry_index = index + 1
            entry = candles_15m[entry_index].open
            if not entry_low <= entry <= entry_high:
                index += 1
                continue
            actual_risk = entry - stop if long else stop - entry
            if actual_risk <= 0:
                index += 1
                continue

            exit_index = min(entry_index + self.max_holding_bars, len(candles_15m) - 1)
            exit_price = candles_15m[exit_index].close
            outcome = "TIME"
            for cursor in range(entry_index, exit_index + 1):
                bar = candles_15m[cursor]
                stop_hit = bar.low <= stop if long else bar.high >= stop
                target_hit = bar.high >= target if long else bar.low <= target
                if stop_hit:  # Conservative if SL and TP occur in the same candle.
                    exit_index, exit_price, outcome = cursor, stop, "SL"
                    break
                if target_hit:
                    exit_index, exit_price, outcome = cursor, target, "TP2"
                    break

            gross_r = (
                (exit_price - entry) / actual_risk if long else (entry - exit_price) / actual_risk
            )
            cost_r = (entry + exit_price) * self.cost_fraction / actual_risk
            result_r = gross_r - cost_r
            records.append(
                TradeRecord(
                    symbol=symbol,
                    entry_time=datetime.fromtimestamp(
                        candles_15m[entry_index].open_time_ms / 1000, UTC
                    ),
                    exit_time=datetime.fromtimestamp(
                        candles_15m[exit_index].open_time_ms / 1000, UTC
                    ),
                    side="LONG" if long else "SHORT",
                    confidence=confidence,
                    required_confidence=required_confidence,
                    score=float(round(score, 2)),
                    regime=regime,
                    entry=float(entry),
                    exit=float(exit_price),
                    stop=float(stop),
                    target=float(target),
                    result_r=float(result_r),
                    outcome=outcome,
                )
            )
            index = exit_index + self.cooldown_bars + 1

        return self._summarize(symbol, candles_15m, records)

    def _summarize(
        self, symbol: str, candles: list[Candle], records: list[TradeRecord]
    ) -> SymbolResearchResult:
        wins = sum(item.result_r > 0 for item in records)
        losses = len(records) - wins
        gross_profit = sum(item.result_r for item in records if item.result_r > 0)
        gross_loss = abs(sum(item.result_r for item in records if item.result_r <= 0))
        equity = self.starting_equity
        peak_equity = equity
        max_drawdown_pct = 0.0
        cumulative_r = 0.0
        peak_r = 0.0
        max_drawdown_r = 0.0
        yearly: dict[str, float] = {}
        for item in records:
            equity *= max(0.01, 1 + self.risk_fraction * item.result_r)
            peak_equity = max(peak_equity, equity)
            max_drawdown_pct = max(max_drawdown_pct, (peak_equity - equity) / peak_equity * 100)
            cumulative_r += item.result_r
            peak_r = max(peak_r, cumulative_r)
            max_drawdown_r = max(max_drawdown_r, peak_r - cumulative_r)
            year = str(item.entry_time.year)
            yearly[year] = yearly.get(year, 0.0) + item.result_r

        confidence_bins: dict[str, dict[str, float]] = {}
        for label, low, high in (("78-80", 78, 80), ("81-83", 81, 83), ("84-89", 84, 89)):
            sample = [item for item in records if low <= item.confidence <= high]
            confidence_bins[label] = {
                "trades": len(sample),
                "win_rate": (
                    sum(item.result_r > 0 for item in sample) / len(sample) * 100 if sample else 0.0
                ),
                "expectancy_r": (
                    sum(item.result_r for item in sample) / len(sample) if sample else 0.0
                ),
            }
        net_r = sum(item.result_r for item in records)
        return SymbolResearchResult(
            symbol=symbol,
            first_bar=datetime.fromtimestamp(candles[0].open_time_ms / 1000, UTC),
            last_bar=datetime.fromtimestamp(candles[-1].open_time_ms / 1000, UTC),
            bars=len(candles),
            trades=len(records),
            wins=wins,
            losses=losses,
            win_rate=wins / len(records) * 100 if records else 0.0,
            net_r=net_r,
            expectancy_r=net_r / len(records) if records else 0.0,
            profit_factor=(
                gross_profit / gross_loss if gross_loss else math.inf if gross_profit else 0.0
            ),
            max_drawdown_r=max_drawdown_r,
            max_drawdown_pct=max_drawdown_pct,
            ending_equity=equity,
            return_pct=(equity / self.starting_equity - 1) * 100,
            yearly_r={key: round(value, 3) for key, value in sorted(yearly.items())},
            confidence_bins=confidence_bins,
            records=tuple(records),
        )
