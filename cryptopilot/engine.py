from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta

import numpy as np

from cryptopilot.config import Settings
from cryptopilot.exchange import INTERVAL_MS
from cryptopilot.indicators import InsufficientData, compute_features, directional_score
from cryptopilot.models import Candle, EarlySetup, FeatureSet, Side, Signal, Ticker, TradePlan


class SignalEngine:
    """Deterministic, explainable multi-timeframe decision engine."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def quick_score(self, candles: list[Candle]) -> float:
        return directional_score(compute_features(candles))

    def quick_early_score(self, candles: list[Candle]) -> float:
        feature = compute_features(candles)
        bias, readiness, _ = self._early_base(feature)
        return readiness if bias is not Side.NO_TRADE else 0.0

    def analyze_early_setup(
        self,
        symbol: str,
        exchange: str,
        ticker: Ticker,
        candles: dict[str, list[Candle]],
        benchmark: list[Candle] | None = None,
    ) -> EarlySetup:
        now = datetime.now(UTC)
        blockers: list[str] = []
        risks: list[str] = []
        features: dict[str, FeatureSet] = {}
        for timeframe in self.settings.timeframe_list:
            try:
                features[timeframe] = compute_features(candles.get(timeframe, []))
            except (InsufficientData, ValueError) as exc:
                blockers.append(f"{timeframe}: недостаточно данных ({exc})")
        if blockers:
            return EarlySetup(
                symbol=symbol,
                exchange=exchange,
                bias=Side.NO_TRADE,
                readiness=0,
                price=ticker.last,
                trigger_price=ticker.last,
                opposite_trigger_price=ticker.last,
                invalidation_price=ticker.last,
                stage="WATCH",
                regime="UNKNOWN",
                created_at=now,
                expires_at=now + timedelta(minutes=self.settings.early_setup_expiry_minutes),
                blockers=blockers[:5],
            )

        ordered = self.settings.timeframe_list
        execution = features[ordered[0]]
        primary = features[ordered[1]]
        structural = features[ordered[-1]]
        benchmark_feature: FeatureSet | None = None
        benchmark_score = 0.0
        regime = "TRANSITION"
        if benchmark:
            try:
                benchmark_feature = compute_features(benchmark)
                benchmark_score = directional_score(benchmark_feature)
                regime = self._market_regime(benchmark_score, benchmark_feature)
            except (InsufficientData, ValueError):
                risks.append("Режим BTC временно недоступен")

        bias, readiness, reasons = self._early_base(primary)
        if bias is Side.NO_TRADE:
            blockers.append("Направление будущего выхода из диапазона пока не подтверждено")
        structural_score = directional_score(structural)
        trend_guard_aligned = (
            (bias is Side.LONG and structural.supertrend_direction > 0)
            or (bias is Side.SHORT and structural.supertrend_direction < 0)
        )
        if structural.adx14 < 18:
            blockers.append("На 4h нет устойчивого тренда для направленного раннего сценария")
        elif (bias is Side.LONG and structural_score < 25) or (
            bias is Side.SHORT and structural_score > -25
        ):
            blockers.append("Давление на 1h не согласовано с направлением тренда 4h")
        if trend_guard_aligned:
            readiness += 8
            reasons.append("Trend Guard (Supertrend + EMA) подтверждает направление на 4h")
        else:
            readiness -= 10
            risks.append("Trend Guard на 4h ещё не подтвердил предполагаемое направление")
        compression_votes = sum(
            (
                primary.keltner_squeeze_ratio < 1.0,
                primary.bb_width_regime_ratio <= 0.9,
                primary.atr_regime_ratio <= 0.9,
                primary.ema_gap_atr <= 0.4,
            )
        )
        if compression_votes < 3:
            blockers.append("Нет подтверждённого сжатия Bollinger/Keltner, ATR и EMA")
        if primary.squeeze_bars < self.settings.early_min_squeeze_bars:
            blockers.append(
                f"Keltner squeeze держится {primary.squeeze_bars} свеч.; "
                f"нужно {self.settings.early_min_squeeze_bars}+"
            )
        if primary.breakout_up or primary.breakout_down:
            blockers.append("Пробой на 1h уже произошёл — для раннего входа поздно")
        if ticker.turnover_24h < self.settings.min_volume_usdt:
            blockers.append("Недостаточная ликвидность для раннего сценария")
        if ticker.spread_bps > self.settings.max_spread_bps:
            blockers.append(f"Спред {ticker.spread_bps:.1f} bps слишком широк")
        data_age = self._data_age(candles[ordered[0]], ordered[0])
        if data_age > INTERVAL_MS[ordered[0]] / 1000 * 2.2:
            blockers.append("Последняя закрытая свеча устарела")

        distance_from_ema = abs(ticker.last - execution.ema20) / max(execution.atr14, 1e-12)
        if distance_from_ema > 1.4:
            blockers.append("Цена уже слишком далеко ушла от EMA20")
        if execution.breakout_up or execution.breakout_down:
            readiness += 6
            reasons.append("На 15m появляется первая активация границы диапазона")
        if primary.cmf20 >= 0.08 and bias is Side.LONG:
            readiness += 5
            reasons.append(f"CMF20 {primary.cmf20:+.2f}: накопление поддерживает LONG")
        elif primary.cmf20 <= -0.08 and bias is Side.SHORT:
            readiness += 5
            reasons.append(f"CMF20 {primary.cmf20:+.2f}: распределение поддерживает SHORT")
        elif (bias is Side.LONG and primary.cmf20 < -0.08) or (
            bias is Side.SHORT and primary.cmf20 > 0.08
        ):
            readiness -= 7
            risks.append(f"CMF20 {primary.cmf20:+.2f} против предполагаемого направления")
        if ticker.open_interest_change_pct is not None:
            if ticker.open_interest_change_pct >= 1.0:
                readiness += min(12, int(5 + ticker.open_interest_change_pct))
                reasons.append(
                    f"Open interest вырос на {ticker.open_interest_change_pct:.1f}% "
                    "при ещё сжатой цене"
                )
            elif ticker.open_interest_change_pct <= -4:
                readiness -= 8
                risks.append("Open interest падает: подготовка движения может быть ложной")
        else:
            risks.append("Изменение open interest недоступно")

        funding_pct = ticker.funding_rate * 100
        if abs(funding_pct) > 0.10:
            readiness -= 10
            risks.append(f"Перегретый funding {funding_pct:+.3f}%")
        flow_aligned = False
        if ticker.taker_buy_ratio is not None:
            flow_aligned = (bias is Side.LONG and ticker.taker_buy_ratio >= 0.56) or (
                bias is Side.SHORT and ticker.taker_buy_ratio <= 0.44
            )
            if flow_aligned:
                readiness += 7
                reasons.append(
                    f"Рыночные покупки taker: {ticker.taker_buy_ratio:.0%} — "
                    "поток подтверждает bias"
                )
            elif (bias is Side.LONG and ticker.taker_buy_ratio <= 0.42) or (
                bias is Side.SHORT and ticker.taker_buy_ratio >= 0.58
            ):
                readiness -= 8
                risks.append(
                    f"Taker buy ratio {ticker.taker_buy_ratio:.0%} против сценария"
                )
        if ticker.orderbook_imbalance is not None:
            book_aligned = (bias is Side.LONG and ticker.orderbook_imbalance >= 0.12) or (
                bias is Side.SHORT and ticker.orderbook_imbalance <= -0.12
            )
            if book_aligned:
                readiness += 4
                reasons.append(
                    f"Дисбаланс стакана {ticker.orderbook_imbalance:+.0%} поддерживает направление"
                )
        if ticker.long_short_ratio is not None and (
            ticker.long_short_ratio >= 2.5 or ticker.long_short_ratio <= 0.4
        ):
            readiness -= 5
            risks.append(
                f"Толпа перекошена: long/short accounts {ticker.long_short_ratio:.2f}"
            )
        if (bias is Side.LONG and regime == "BEAR") or (
            bias is Side.SHORT and regime == "BULL"
        ):
            readiness -= 8
            risks.append("Предполагаемое направление против текущего режима BTC")
        elif (bias is Side.LONG and regime == "BULL") or (
            bias is Side.SHORT and regime == "BEAR"
        ):
            readiness += 5
            reasons.append(f"Режим BTC {regime} поддерживает направление")

        if benchmark_feature is not None and symbol != "BTCUSDT":
            relative_edge = structural.return_20_pct - benchmark_feature.return_20_pct
            if (bias is Side.LONG and relative_edge > 0) or (
                bias is Side.SHORT and relative_edge < 0
            ):
                readiness += 5
                reasons.append(
                    f"Относительная сила к BTC подтверждает bias ({relative_edge:+.1f}%)"
                )
            else:
                risks.append(
                    "Относительная сила к BTC пока не подтверждает bias "
                    f"({relative_edge:+.1f}%)"
                )

        readiness = int(np.clip(readiness, 0, 95))
        if readiness < self.settings.min_early_readiness:
            blockers.append(
                f"Готовность {readiness}/100 ниже минимума "
                f"{self.settings.min_early_readiness}"
            )
        trigger = primary.range_high20 if bias is Side.LONG else primary.range_low20
        opposite_trigger = primary.range_low20 if bias is Side.LONG else primary.range_high20
        execution_activation = (
            (bias is Side.LONG and execution.breakout_up)
            or (bias is Side.SHORT and execution.breakout_down)
        )
        stage = (
            "CONFIRMED_WATCH"
            if execution_activation
            and execution.relative_volume20 >= 1.15
            and (ticker.taker_buy_ratio is None or flow_aligned)
            and trend_guard_aligned
            else "WATCH"
        )
        invalidation = (
            min(primary.ema50, primary.range_low20)
            if bias is Side.LONG
            else max(primary.ema50, primary.range_high20)
        )
        return EarlySetup(
            symbol=symbol,
            exchange=exchange,
            bias=bias if not blockers else Side.NO_TRADE,
            readiness=readiness,
            price=ticker.last,
            trigger_price=trigger,
            opposite_trigger_price=opposite_trigger,
            invalidation_price=invalidation,
            stage=stage,
            regime=regime,
            created_at=now,
            expires_at=now + timedelta(minutes=self.settings.early_setup_expiry_minutes),
            reasons=reasons[:7],
            risks=risks[:5],
            blockers=blockers[:5],
            metrics={
                "keltner_squeeze_ratio": primary.keltner_squeeze_ratio,
                "squeeze_bars": float(primary.squeeze_bars),
                "choppiness14": primary.choppiness14,
                "cmf20": primary.cmf20,
                "relative_volume20": execution.relative_volume20,
                "taker_buy_ratio": ticker.taker_buy_ratio
                if ticker.taker_buy_ratio is not None
                else -1.0,
                "orderbook_imbalance": ticker.orderbook_imbalance
                if ticker.orderbook_imbalance is not None
                else -2.0,
                "supertrend_1h": float(primary.supertrend_direction),
                "supertrend_4h": float(structural.supertrend_direction),
            },
        )

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

        regime = "TRANSITION"
        benchmark_score = 0.0
        benchmark_feature: FeatureSet | None = None
        if benchmark:
            try:
                benchmark_feature = compute_features(benchmark)
                benchmark_score = directional_score(benchmark_feature)
                regime = self._market_regime(benchmark_score, benchmark_feature)
            except (InsufficientData, ValueError):
                risks.append("Режим BTC недоступен; уверенность снижена")

        primary = features[ordered[1]]
        execution = features[ordered[0]]
        structural = features[ordered[-1]]
        data_age = self._data_age(candles[ordered[0]], ordered[0])
        side = Side.LONG if score > 0 else Side.SHORT

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
        if primary.adx14 < self.settings.min_primary_adx:
            blockers.append(
                f"ADX {primary.adx14:.1f} ниже адаптивного минимума "
                f"{self.settings.min_primary_adx:.0f}: тренд недостаточно устойчив"
            )
        if primary.efficiency_ratio20 < self.settings.min_efficiency_ratio:
            blockers.append(
                f"Рынок слишком шумный: efficiency ratio "
                f"{primary.efficiency_ratio20:.2f}"
            )
        if primary.ema_gap_atr < self.settings.min_ema_gap_atr:
            blockers.append("EMA20 и EMA50 слишком близко: вероятна переходная фаза")
        if primary.atr_regime_ratio > self.settings.max_atr_regime_ratio:
            blockers.append(
                f"Всплеск волатильности {primary.atr_regime_ratio:.1f}× нормы; "
                "вход временно заблокирован"
            )
        if side is Side.LONG and primary.dmi_spread < -self.settings.max_countertrend_dmi:
            blockers.append("DMI на 1h не подтверждает давление покупателей")
        if side is Side.SHORT and primary.dmi_spread > self.settings.max_countertrend_dmi:
            blockers.append("DMI на 1h не подтверждает давление продавцов")

        threshold = 45.0
        if abs(score) < threshold:
            blockers.append(f"Совокупный edge слабый: {abs(score):.1f}/100, нужно {threshold:.0f}+")

        if side is Side.LONG and regime == "BEAR" and benchmark_score < -45:
            blockers.append("Сильный медвежий режим BTC против LONG")
        if side is Side.SHORT and regime == "BULL" and benchmark_score > 45:
            blockers.append("Сильный бычий режим BTC против SHORT")

        if (
            self.settings.relative_strength_filter
            and symbol != "BTCUSDT"
            and benchmark_feature is not None
        ):
            relative_edge = structural.return_20_pct - benchmark_feature.return_20_pct
            tolerance = max(1.0, benchmark_feature.atr_pct * 1.5)
            if side is Side.LONG and relative_edge < -tolerance:
                blockers.append(
                    f"Монета слабее BTC на {abs(relative_edge):.1f}% за окно 4h"
                )
            elif side is Side.SHORT and relative_edge > tolerance:
                blockers.append(
                    f"Монета сильнее BTC на {relative_edge:.1f}% — SHORT не подтверждён"
                )

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
        if ticker.open_interest_change_pct is not None and ticker.open_interest_change_pct >= 2:
            reasons.append(
                f"Open interest растёт на {ticker.open_interest_change_pct:.1f}%: "
                "движение поддержано новыми позициями"
            )
        risks.extend(self._risks(side, primary, execution, ticker))
        self._append_microstructure_context(side, ticker, reasons, risks)

        if blockers:
            result = self._no_trade(symbol, exchange, ticker.last, now, blockers, features)
            result.score = round(score, 1)
            result.regime = regime
            result.reasons = reasons[:5]
            result.risks = risks[:4]
            result.data_age_seconds = data_age
            return result

        confidence = self._confidence(score, aligned, regime, side, ticker, primary, risks)
        required_confidence = (
            self.settings.min_auto_confidence
            if side is Side.LONG
            else self.settings.min_auto_confidence_short
        )
        if regime in {"RANGE", "TRANSITION"}:
            required_confidence = min(
                95, required_confidence + self.settings.neutral_regime_confidence_penalty
            )
        if confidence < self.settings.min_manual_confidence:
            blockers.append(
                f"Качество сетапа {confidence}/100 ниже ручного минимума "
                f"{self.settings.min_manual_confidence}"
            )
            result = self._no_trade(symbol, exchange, ticker.last, now, blockers, features)
            result.score = round(score, 1)
            result.confidence = confidence
            result.required_confidence = required_confidence
            result.regime = regime
            result.reasons = reasons[:6]
            result.risks = risks[:4]
            result.data_age_seconds = data_age
            return result

        risk_multiplier = 1.0
        if regime in {"RANGE", "TRANSITION"}:
            risk_multiplier = 0.5
        elif confidence < required_confidence + 2:
            risk_multiplier = 0.75
        plan = self._build_plan(
            side, ticker.last, candles[ordered[0]], execution, now, risk_multiplier
        )
        stop_pct = abs(ticker.last - plan.stop_loss) / ticker.last * 100
        if stop_pct < 0.25 or stop_pct > 5.0:
            blockers.append(f"Технический стоп {stop_pct:.2f}% вне допустимого диапазона 0.25–5%")
            result = self._no_trade(symbol, exchange, ticker.last, now, blockers, features)
            result.score = round(score, 1)
            result.confidence = confidence
            result.required_confidence = required_confidence
            result.regime = regime
            result.reasons = reasons[:5]
            result.risks = risks[:4]
            result.data_age_seconds = data_age
            return result

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
            required_confidence=required_confidence,
            market_context=self._market_context(ticker),
        )

    @staticmethod
    def _weights(count: int) -> tuple[float, ...]:
        if count == 3:
            return (0.25, 0.35, 0.40)
        raw = np.arange(1, count + 1, dtype=float)
        normalized = raw / raw.sum()
        return tuple(float(x) for x in normalized)

    @staticmethod
    def _market_regime(score: float, feature: FeatureSet) -> str:
        if feature.adx14 < 16:
            return "RANGE"
        if score >= 25:
            return "BULL"
        if score <= -25:
            return "BEAR"
        return "TRANSITION"

    @staticmethod
    def _early_base(feature: FeatureSet) -> tuple[Side, int, list[str]]:
        readiness = 32
        reasons: list[str] = []
        if feature.bb_width_regime_ratio <= 0.75:
            readiness += 18
            reasons.append(
                f"Bollinger width сжат до {feature.bb_width_regime_ratio:.2f}× нормы"
            )
        elif feature.bb_width_regime_ratio <= 0.9:
            readiness += 10
            reasons.append("Полосы Bollinger заметно сужаются")
        if feature.atr_regime_ratio <= 0.8:
            readiness += 14
            reasons.append(f"ATR сжат до {feature.atr_regime_ratio:.2f}× нормы")
        elif feature.atr_regime_ratio <= 0.9:
            readiness += 8
            reasons.append("Текущая волатильность ниже обычной")
        if feature.ema_gap_atr <= 0.25:
            readiness += 12
            reasons.append("EMA20/50 сжаты — энергия движения ещё не реализована")
        elif feature.ema_gap_atr <= 0.4:
            readiness += 6
            reasons.append("EMA20/50 сходятся перед возможным расширением")
        if feature.adx14 <= 24:
            readiness += 5
            reasons.append(f"ADX {feature.adx14:.1f}: тренд ещё не перегрет")
        if feature.keltner_squeeze_ratio < 1:
            readiness += min(10, 4 + feature.squeeze_bars)
            reasons.append(
                f"Bollinger внутри Keltner {feature.squeeze_bars} свеч. "
                f"(ratio {feature.keltner_squeeze_ratio:.2f})"
            )

        votes = 0
        votes += (
            1
            if feature.ema20_slope_pct > 0.02
            else -1
            if feature.ema20_slope_pct < -0.02
            else 0
        )
        votes += 1 if feature.dmi_spread > 3 else -1 if feature.dmi_spread < -3 else 0
        votes += 1 if feature.macd_hist > 0 else -1
        votes += (
            1
            if feature.range_position20 >= 0.58
            else -1
            if feature.range_position20 <= 0.42
            else 0
        )
        votes += 1 if feature.close >= feature.ema20 else -1
        votes += 1 if feature.cmf20 > 0.05 else -1 if feature.cmf20 < -0.05 else 0
        votes += 1 if feature.supertrend_direction > 0 else -1
        bias = Side.LONG if votes >= 2 else Side.SHORT if votes <= -2 else Side.NO_TRADE
        if bias is Side.LONG:
            reasons.append(
                f"Цена поджимается к верхней части диапазона ({feature.range_position20:.0%})"
            )
        elif bias is Side.SHORT:
            reasons.append(
                f"Цена удерживается в нижней части диапазона ({feature.range_position20:.0%})"
            )
        readiness += min(10, abs(votes) * 2)
        return bias, readiness, reasons

    @staticmethod
    def _market_context(ticker: Ticker) -> dict[str, float]:
        values = {
            "funding_pct": ticker.funding_rate * 100,
            "spread_bps": ticker.spread_bps,
        }
        optional = {
            "oi_change_pct": ticker.open_interest_change_pct,
            "taker_buy_ratio": ticker.taker_buy_ratio,
            "orderbook_imbalance": ticker.orderbook_imbalance,
            "long_short_ratio": ticker.long_short_ratio,
        }
        values.update({key: value for key, value in optional.items() if value is not None})
        return values

    @staticmethod
    def _append_microstructure_context(
        side: Side, ticker: Ticker, reasons: list[str], risks: list[str]
    ) -> None:
        if ticker.taker_buy_ratio is not None:
            aligned = (side is Side.LONG and ticker.taker_buy_ratio >= 0.56) or (
                side is Side.SHORT and ticker.taker_buy_ratio <= 0.44
            )
            opposed = (side is Side.LONG and ticker.taker_buy_ratio <= 0.42) or (
                side is Side.SHORT and ticker.taker_buy_ratio >= 0.58
            )
            if aligned:
                reasons.append(f"Taker buy ratio {ticker.taker_buy_ratio:.0%} подтверждает поток")
            elif opposed:
                risks.append(f"Taker buy ratio {ticker.taker_buy_ratio:.0%} против направления")
        if ticker.orderbook_imbalance is not None:
            aligned = (side is Side.LONG and ticker.orderbook_imbalance >= 0.12) or (
                side is Side.SHORT and ticker.orderbook_imbalance <= -0.12
            )
            if aligned:
                reasons.append(f"Дисбаланс стакана {ticker.orderbook_imbalance:+.0%} по тренду")
            elif abs(ticker.orderbook_imbalance) >= 0.25:
                risks.append(
                    f"Снимок стакана перекошен {ticker.orderbook_imbalance:+.0%}; "
                    "возможна ликвидность-приманка"
                )
        if ticker.long_short_ratio is not None and (
            ticker.long_short_ratio >= 2.5 or ticker.long_short_ratio <= 0.4
        ):
            risks.append(f"Экстремум long/short accounts {ticker.long_short_ratio:.2f}")

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
            f"ADX {primary.adx14:.1f}, DMI spread {primary.dmi_spread:+.1f}, "
            f"efficiency {primary.efficiency_ratio20:.2f}",
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
        elif (
            ticker.open_interest_change_pct is not None
            and ticker.open_interest_change_pct <= -5
        ):
            risks.append(
                f"Open interest снизился на {abs(ticker.open_interest_change_pct):.1f}%: "
                "движение может быть закрытием позиций"
            )
        return risks

    def _build_plan(
        self,
        side: Side,
        price: float,
        candles: list[Candle],
        feature: FeatureSet,
        now: datetime,
        risk_multiplier: float = 1.0,
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

        risk_amount = (
            self.settings.account_equity_usdt
            * self.settings.risk_per_trade_pct
            / 100
            * risk_multiplier
        )
        theoretical_notional = risk_amount / max(distance / price, 1e-12)
        cap = self.settings.account_equity_usdt * self.settings.max_position_pct / 100
        recommended_leverage = (
            min(self.settings.preferred_leverage, self.settings.max_leverage)
            if feature.atr_pct <= 2.5 and risk_multiplier >= 0.75
            else 1
        )
        cap *= recommended_leverage
        notional = min(theoretical_notional, cap)
        if side is Side.LONG:
            scale_entries = (
                price,
                max(price - 0.35 * atr_value, stop + 0.55 * distance),
                max(price - 0.70 * atr_value, stop + 0.30 * distance),
            )
        else:
            scale_entries = (
                price,
                min(price + 0.35 * atr_value, stop - 0.55 * distance),
                min(price + 0.70 * atr_value, stop - 0.30 * distance),
            )
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
            scale_entries=tuple(float(item) for item in scale_entries),
            scale_allocations_pct=(50, 30, 20),
            recommended_leverage=recommended_leverage,
            max_leverage=self.settings.max_leverage,
            holding_horizon="1–72 часа; выход раньше при сломе структуры",
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
        if ticker.taker_buy_ratio is not None and (
            (side is Side.LONG and ticker.taker_buy_ratio >= 0.56)
            or (side is Side.SHORT and ticker.taker_buy_ratio <= 0.44)
        ):
            value += 2
        if ticker.orderbook_imbalance is not None and (
            (side is Side.LONG and ticker.orderbook_imbalance >= 0.12)
            or (side is Side.SHORT and ticker.orderbook_imbalance <= -0.12)
        ):
            value += 1
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
