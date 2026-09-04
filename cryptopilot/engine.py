from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta

import numpy as np

from cryptopilot.config import Settings
from cryptopilot.exchange import INTERVAL_MS
from cryptopilot.indicators import InsufficientData, compute_features, directional_score
from cryptopilot.models import Candle, FeatureSet, Side, Signal, Ticker, TradePlan


class SignalEngine:
    """Deterministic, explainable multi-timeframe decision engine."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def quick_score(self, candles: list[Candle]) -> float:
        return directional_score(compute_features(candles))

    def analyze(
        self,
        symbol: str,
        exchange: str,
        ticker: Ticker,
        candles: dict[str, list[Candle]],
        benchmark: list[Candle] | None = None,
    ) -> Signal:
        now = datetime.now(UTC)
        blockers: list[str] = []
        reasons: list[str] = []
        risks: list[str] = []
        features: dict[str, FeatureSet] = {}
        scores: dict[str, float] = {}

        for timeframe in self.settings.timeframe_list:
            series = candles.get(timeframe, [])
            try:
                features[timeframe] = compute_features(series)
                scores[timeframe] = directional_score(features[timeframe])
            except (InsufficientData, ValueError) as exc:
                blockers.append(f"{timeframe}: недостаточно качественных данных ({exc})")

        if blockers:
            return self._no_trade(symbol, exchange, ticker.last, now, blockers, features)

        ordered = self.settings.timeframe_list
        weights = self._weights(len(ordered))
        score = sum(
            scores[timeframe] * weight for timeframe, weight in zip(ordered, weights, strict=True)
        )
        directions = [int(np.sign(scores[item])) for item in ordered]
        aligned = len(set(directions)) == 1 and directions[0] != 0
        higher_aligned = directions[-1] == directions[-2] != 0

        regime = "NEUTRAL"
        benchmark_score = 0.0
        if benchmark:
            try:
                benchmark_score = directional_score(compute_features(benchmark))
                regime = (
                    "BULL"
                    if benchmark_score >= 25
                    else "BEAR"
                    if benchmark_score <= -25
                    else "NEUTRAL"
                )
            except (InsufficientData, ValueError):
                risks.append("Режим BTC недоступен; уверенность снижена")

        primary = features[ordered[1]]
        execution = features[ordered[0]]
        data_age = self._data_age(candles[ordered[0]], ordered[0])

        if ticker.turnover_24h < self.settings.min_volume_usdt:
            blockers.append("Суточный оборот ниже фильтра ликвидности")
        if ticker.spread_bps > self.settings.max_spread_bps:
            blockers.append(f"Спред {ticker.spread_bps:.1f} bps слишком широк")
        if data_age > INTERVAL_MS[ordered[0]] / 1000 * 2.2:
            blockers.append("Последняя закрытая свеча устарела")
        if not higher_aligned:
            blockers.append("Старшие таймфреймы не подтверждают одно направление")
        if primary.atr_pct < 0.22:
            blockers.append("Волатильность слишком низкая для разумной цели")
        if primary.atr_pct > 7.5:
            blockers.append("Аномально высокая волатильность; стоп получается ненадёжным")

        side = Side.LONG if score > 0 else Side.SHORT
        threshold = 45.0
        if abs(score) < threshold:
            blockers.append(f"Совокупный edge слабый: {abs(score):.1f}/100, нужно {threshold:.0f}+")

        if side is Side.LONG and regime == "BEAR" and benchmark_score < -45:
            blockers.append("Сильный медвежий режим BTC против LONG")
        if side is Side.SHORT and regime == "BULL" and benchmark_score > 45:
            blockers.append("Сильный бычий режим BTC против SHORT")

        distance_from_ema = abs(ticker.last - execution.ema20) / max(execution.atr14, 1e-12)
        if distance_from_ema > 2.4:
            blockers.append("Цена слишком далеко от EMA20: вход означал бы погоню за движением")

        funding_pct = ticker.funding_rate * 100
        if side is Side.LONG and funding_pct > 0.10:
            blockers.append("Funding перегрет в пользу LONG")
        elif side is Side.SHORT and funding_pct < -0.10:
            blockers.append("Funding перегрет в пользу SHORT")
        elif abs(funding_pct) > 0.05:
            risks.append(f"Повышенный funding: {funding_pct:+.3f}%")

        reasons.extend(self._reasons(side, scores, features, aligned, regime))
        risks.extend(self._risks(side, primary, execution, ticker))

        if blockers:
            result = self._no_trade(symbol, exchange, ticker.last, now, blockers, features)
            result.score = round(score, 1)
            result.regime = regime
            result.reasons = reasons[:5]
            result.risks = risks[:4]
            result.data_age_seconds = data_age
            return result

        plan = self._build_plan(side, ticker.last, candles[ordered[0]], execution, now)
        stop_pct = abs(ticker.last - plan.stop_loss) / ticker.last * 100
        if stop_pct < 0.25 or stop_pct > 5.0:
            blockers.append(f"Технический стоп {stop_pct:.2f}% вне допустимого диапазона 0.25–5%")
            result = self._no_trade(symbol, exchange, ticker.last, now, blockers, features)
            result.score = round(score, 1)
            result.regime = regime
            result.reasons = reasons[:5]
            result.risks = risks[:4]
            result.data_age_seconds = data_age
            return result

        confidence = self._confidence(score, aligned, regime, side, ticker, primary, risks)
        return Signal(
            symbol=symbol,
            exchange=exchange,
            side=side,
            confidence=confidence,
            score=round(score, 1),
            regime=regime,
            price=ticker.last,
            created_at=now,
            reasons=reasons[:6],
            risks=risks[:4],
            features=features,
            plan=plan,
            data_age_seconds=data_age,
        )

    @staticmethod
    def _weights(count: int) -> tuple[float, ...]:
        if count == 3:
            return (0.25, 0.35, 0.40)
        raw = np.arange(1, count + 1, dtype=float)
        normalized = raw / raw.sum()
        return tuple(float(x) for x in normalized)

    @staticmethod
    def _data_age(candles: list[Candle], timeframe: str) -> int:
        if not candles:
            return 2**31
        closed_at = candles[-1].open_time_ms + INTERVAL_MS[timeframe]
        return max(0, int(time.time() - closed_at / 1000))

    @staticmethod
    def _reasons(
        side: Side,
        scores: dict[str, float],
        features: dict[str, FeatureSet],
        aligned: bool,
        regime: str,
    ) -> list[str]:
        direction = "бычье" if side is Side.LONG else "медвежье"
        ordered = list(scores)
        primary = features[ordered[1]]
        execution = features[ordered[0]]
        result = [
            f"Взвешенное {direction} подтверждение на "
            + ", ".join(f"{tf}={scores[tf]:+.0f}" for tf in ordered),
            f"EMA20/50/200 и наклон тренда подтверждают направление; ADX {primary.adx14:.1f}",
            f"RSI14 {primary.rsi14:.1f}, MACD histogram {primary.macd_hist:+.6g}",
        ]
        if aligned:
            result.append("Все выбранные таймфреймы направлены одинаково")
        if execution.breakout_up or execution.breakout_down:
            result.append("Есть пробой 20-свечной структуры на входном таймфрейме")
        if execution.volume_z > 1:
            result.append(f"Объём подтверждает импульс: z-score {execution.volume_z:.1f}")
        result.append(f"Рыночный режим BTC: {regime}")
        return result

    @staticmethod
    def _risks(side: Side, primary: FeatureSet, execution: FeatureSet, ticker: Ticker) -> list[str]:
        risks: list[str] = []
        if primary.adx14 < 22:
            risks.append("ADX невысок: рынок может перейти во флэт")
        if side is Side.LONG and execution.rsi14 > 70:
            risks.append("Краткосрочная перекупленность повышает риск отката")
        if side is Side.SHORT and execution.rsi14 < 30:
            risks.append("Краткосрочная перепроданность повышает риск отскока")
        if execution.volume_z < -0.5:
            risks.append("Импульс не поддержан текущим объёмом")
        if ticker.open_interest <= 0:
            risks.append("Open interest недоступен и не участвует в подтверждении")
        return risks

    def _build_plan(
        self,
        side: Side,
        price: float,
        candles: list[Candle],
        feature: FeatureSet,
        now: datetime,
    ) -> TradePlan:
        atr_value = feature.atr14
        swing_low = min(x.low for x in candles[-18:])
        swing_high = max(x.high for x in candles[-18:])
        if side is Side.LONG:
            entry_low, entry_high = price - 0.18 * atr_value, price + 0.05 * atr_value
            stop = min(swing_low, price - 1.45 * atr_value)
            distance = price - stop
            targets = (
                price + distance,
                price + max(2.0, self.settings.min_risk_reward) * distance,
                price + 3 * distance,
            )
            invalidation = "Закрытие свечи ниже стопа или слом структуры EMA50"
        else:
            entry_low, entry_high = price - 0.05 * atr_value, price + 0.18 * atr_value
            stop = max(swing_high, price + 1.45 * atr_value)
            distance = stop - price
            targets = (
                price - distance,
                price - max(2.0, self.settings.min_risk_reward) * distance,
                price - 3 * distance,
            )
            invalidation = "Закрытие свечи выше стопа или слом структуры EMA50"

        risk_amount = self.settings.account_equity_usdt * self.settings.risk_per_trade_pct / 100
        theoretical_notional = risk_amount / max(distance / price, 1e-12)
        cap = self.settings.account_equity_usdt * self.settings.max_position_pct / 100
        notional = min(theoretical_notional, cap)
        return TradePlan(
            entry_low=float(entry_low),
            entry_high=float(entry_high),
            stop_loss=float(stop),
            take_profit_1=float(targets[0]),
            take_profit_2=float(targets[1]),
            take_profit_3=float(targets[2]),
            risk_reward_2=max(2.0, self.settings.min_risk_reward),
            invalidation=invalidation,
            expires_at=now + timedelta(minutes=self.settings.signal_expiry_minutes),
            suggested_notional=float(notional),
            suggested_quantity=float(notional / price),
            risk_amount=float(min(risk_amount, notional * distance / price)),
        )

    @staticmethod
    def _confidence(
        score: float,
        aligned: bool,
        regime: str,
        side: Side,
        ticker: Ticker,
        primary: FeatureSet,
        risks: list[str],
    ) -> int:
        value = 50 + abs(score) * 0.36
        value += 5 if aligned else 0
        value += (
            3
            if (side is Side.LONG and regime == "BULL") or (side is Side.SHORT and regime == "BEAR")
            else 0
        )
        value += 2 if primary.adx14 >= 25 else 0
        value += 2 if ticker.turnover_24h >= 100_000_000 else 0
        value -= min(8, len(risks) * 2)
        return int(np.clip(round(value), 50, 89))

    @staticmethod
    def _no_trade(
        symbol: str,
        exchange: str,
        price: float,
        now: datetime,
        blockers: list[str],
        features: dict[str, FeatureSet],
    ) -> Signal:
        return Signal(
            symbol=symbol,
            exchange=exchange,
            side=Side.NO_TRADE,
            confidence=0,
            score=0.0,
            regime="UNKNOWN",
            price=price if math.isfinite(price) else 0,
            created_at=now,
            blockers=blockers[:6],
            features=features,
        )
