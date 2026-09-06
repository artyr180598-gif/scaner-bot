from __future__ import annotations

import math
from datetime import UTC, datetime

from cryptopilot.indicators import compute_features, directional_score
from cryptopilot.models import BacktestResult, Candle


class WalkForwardBacktester:
    """Conservative next-bar backtest for the live engine's core trend rules.

    Every decision uses only candles closed before the simulated entry. If target and stop
    are both touched inside one candle, the stop is assumed to hit first.
    """

    def run(
        self,
        symbol: str,
        timeframe: str,
        candles: list[Candle],
        score_threshold: float = 45,
        max_holding_bars: int = 48,
        one_way_cost_bps: float = 6.0,
    ) -> BacktestResult:
        if len(candles) < 260:
            raise ValueError("Backtest needs at least 260 closed candles")
        if max_holding_bars < 1 or not math.isfinite(one_way_cost_bps) or one_way_cost_bps < 0:
            raise ValueError("Holding period must be positive and costs finite/nonnegative")
        results_r: list[float] = []
        index = 220
        while index < len(candles) - 2:
            history = candles[: index + 1]
            feature = compute_features(history)
            score = directional_score(feature)
            if abs(score) < score_threshold:
                index += 1
                continue

            long = score > 0
            entry_index = index + 1
            entry = candles[entry_index].open
            swing_low = min(x.low for x in history[-18:])
            swing_high = max(x.high for x in history[-18:])
            stop = (
                min(swing_low, entry - 1.45 * feature.atr14)
                if long
                else max(swing_high, entry + 1.45 * feature.atr14)
            )
            distance = entry - stop if long else stop - entry
            if distance <= 0 or distance / entry < 0.0025 or distance / entry > 0.05:
                index += 1
                continue
            target = entry + 2 * distance if long else entry - 2 * distance

            exit_index = min(entry_index + max_holding_bars - 1, len(candles) - 1)
            exit_price = candles[exit_index].close
            for cursor in range(entry_index, exit_index + 1):
                bar = candles[cursor]
                stop_hit = bar.low <= stop if long else bar.high >= stop
                target_hit = bar.high >= target if long else bar.low <= target
                if stop_hit:
                    exit_price = min(stop, bar.open) if long else max(stop, bar.open)
                    exit_index = cursor
                    break
                if target_hit:
                    exit_price = target
                    exit_index = cursor
                    break
            gross_r = ((exit_price - entry) if long else (entry - exit_price)) / distance
            # Fees/slippage scale with notional, not a fixed fraction of stop risk.
            cost_r = (entry + exit_price) * (one_way_cost_bps / 10_000) / distance
            results_r.append(gross_r - cost_r)
            index = exit_index + 1

        wins = sum(value > 0 for value in results_r)
        losses = sum(value <= 0 for value in results_r)
        gross_profit = sum(value for value in results_r if value > 0)
        gross_loss = abs(sum(value for value in results_r if value <= 0))
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in results_r:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            bars=len(candles),
            trades=len(results_r),
            wins=wins,
            losses=losses,
            win_rate=(wins / len(results_r) * 100) if results_r else 0.0,
            expectancy_r=(sum(results_r) / len(results_r)) if results_r else 0.0,
            profit_factor=(gross_profit / gross_loss)
            if gross_loss
            else (math.inf if gross_profit else 0),
            max_drawdown_r=max_drawdown,
            started_at=datetime.fromtimestamp(candles[0].open_time_ms / 1000, UTC),
            finished_at=datetime.fromtimestamp(candles[-1].open_time_ms / 1000, UTC),
        )
