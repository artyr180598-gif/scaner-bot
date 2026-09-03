"""Signal engine and scanner. Produces real, evidence-based trade ideas."""

from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from loguru import logger

from cryptoforge_pro.analysis import indicators as ta
from cryptoforge_pro.config import Settings
from cryptoforge_pro.market import MarketService
from cryptoforge_pro.models import Candle, MarketData, Signal, TickerInfo

TIMEFRAMES_ALL = ["1h", "4h", "1d"]
TIMEFRAMES_SCALP = ["15m", "1h"]
TIMEFRAMES_SWING = ["4h", "1d"]
TIMEFRAMES_FALLBACK = ["1h", "4h"]


@dataclass(slots=True)
class FactorSnapshot:
    trend: float = 0.0
    momentum: float = 0.0
    volume: float = 0.0
    structure: float = 0.0
    volatility: float = 0.0
    score: float = 0.0
    conviction: float = 0.0


class SignalEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_signal(
        self,
        data: MarketData,
        *,
        mode: str = "best",
        direction: Optional[str] = None,
        risk_profile: str = "balanced",
    ) -> Optional[Signal]:
        """Build a single signal from real market data.

        Returns None when the setup quality is below the configured confidence
        for the user's risk profile.
        """
        if not data.candles:
            return None
        tf_factors: dict[str, FactorSnapshot] = {}
        for tf, candles in data.candles.items():
            if len(candles) < 40:
                continue
            snapshot = self._factor_snapshot(candles, data, tf)
            tf_factors[tf] = snapshot

        if not tf_factors:
            return None

        # Timeframe weights: prefer the higher-timeframe structure for swing,
        # prefer fast timeframe conviction for scalp.
        weights = self._timeframe_weights(tf_factors.keys(), mode)
        bias = sum(s.score * weights.get(tf, 1.0) for tf, s in tf_factors.items())
        total_w = sum(weights.get(tf, 1.0) for tf in tf_factors)
        if total_w <= 0:
            return None
        bias /= total_w

        if direction:
            direction = direction.upper()
            if direction not in ("LONG", "SHORT"):
                return None
        else:
            direction = "LONG" if bias >= 0 else "SHORT"

        # Factor agreement and conviction.
        agreement, conviction = self._agreement(tf_factors, direction)

        # 24h real change confirms the direction (spot ticker).
        ticker_change = data.ticker.change_24h_pct if data.ticker else 0.0
        if ticker_change > 1.0 and direction == "SHORT":
            agreement -= 0.5
        if ticker_change < -1.0 and direction == "LONG":
            agreement -= 0.5

        # Derivatives confirmation (only when real data exists).
        der_bonus = 0.0
        der_text_extra: list[str] = []
        der = data.derivatives
        if der and der.funding_rate is not None:
            fr = der.funding_rate
            if direction == "LONG" and fr >= 0:
                der_bonus += 0.6
                der_text_extra.append(f"funding {fr * 100:.4f}%/8h confirms long pressure")
            elif direction == "SHORT" and fr < 0:
                der_bonus += 0.6
                der_text_extra.append(f"funding {fr * 100:.4f}%/8h confirms short pressure")
            elif abs(fr) > 0.0015:
                # crowded opposite direction is a risk
                der_bonus -= 0.8
                der_text_extra.append(
                    f"funding {fr * 100:.4f}%/8h is crowded against your side"
                )
        if der and der.open_interest_value_usd:
            der_bonus += 0.2
        if der and der.open_interest:
            der_bonus += 0.2

        # BTC correlation: if highly correlated, direction conflicts are risk.
        if abs(data.btc_corr_returns) > 0.75 and data.btc_change_24h_pct != 0:
            if (data.btc_change_24h_pct > 1.0) != (direction == "LONG"):
                der_bonus -= 0.7
                der_text_extra.append("BTC is strongly correlated and moving against this idea")

        score_avg = sum(conviction for s in tf_factors.values()) / max(len(tf_factors), 1)
        confidence = 50.0 + 25.0 * abs(score_avg) + 8.0 * agreement + 4.0 * der_bonus
        # Small bonus when a higher timeframe agrees with the entry timeframe.
        if len(tf_factors) > 1:
            confidence += 2.0
        confidence = max(40.0, min(96.0, confidence))

        threshold = self._threshold(risk_profile)
        if threshold <= 0:
            threshold = self.settings.min_confidence
        if confidence < threshold:
            return None

        plan = self._plan(data, direction)
        if plan is None:
            return None

        factor_text = self._rationale(data, tf_factors, direction, der_text_extra)
        risks = self._risks(data, direction, plan["atr_pct"])
        return Signal(
            symbol=data.symbol,
            base=data.base,
            quote=data.quote,
            direction=direction,
            timeframe=self._display_timeframe(tf_factors, mode),
            entry_low=plan["entry_low"],
            entry_high=plan["entry_high"],
            stop_loss=plan["stop_loss"],
            tp1=plan["tp1"],
            tp2=plan["tp2"],
            tp3=plan["tp3"],
            rr1=plan["rr"][0],
            rr2=plan["rr"][1],
            rr3=plan["rr"][2],
            confidence=round(confidence, 0),
            rationale=factor_text,
            risks=risks,
            score=round(confidence, 1),
            price=data.price,
            atr_pct=plan["atr_pct"],
            factors={f"{tf}:{fact}": val for tf, s in tf_factors.items() for fact, val in asdict(s).items()},
            market=data,
        )

    def _factor_snapshot(self, candles: list[Candle], data: MarketData, tf: str) -> FactorSnapshot:
        closes = [c.close for c in candles]
        price = closes[-1] if closes else data.price
        rsi_v = ta.rsi(closes, 14) or 50.0
        ema20 = ta.ema(closes, 20) or price
        ema50 = ta.ema(closes, 50) or price
        macd_hist = ta.macd(closes) or 0.0
        vol_ratio = ta.volume_ratio(candles, 20) or 1.0
        atr_v = ta.atr(candles, 14)
        atr_pct = atr_v / price * 100.0 if atr_v and price else 0.0
        slope10 = ta.slope_pct(candles, 10) or 0.0
        slope20 = ta.slope_pct(candles, 20) or 0.0

        # trend: ema20>ema50 and rising
        trend = 0.0
        if ema20 > ema50:
            trend += 0.8
        else:
            trend -= 0.8
        if slope20 > 1.0:
            trend += 0.5
        elif slope20 < -1.0:
            trend -= 0.5
        trend += max(-2, min(2, slope10 / 2.0))
        trend = max(-1, min(1, trend / 2.2))

        # momentum: RSI + MACD
        momentum = 0.0
        if rsi_v > 55:
            momentum += 0.7
        elif rsi_v < 45:
            momentum -= 0.7
        if 45 <= rsi_v <= 60:
            momentum += 0.2
        if macd_hist > 0:
            momentum += 0.8
        elif macd_hist < 0:
            momentum -= 0.8
        momentum = max(-1, min(1, momentum / 1.6))

        # volume: strong relative volume supports any move
        volume = 0.0
        if vol_ratio and vol_ratio >= 1.5:
            volume += 0.8
        elif vol_ratio and vol_ratio >= 1.1:
            volume += 0.4
        elif vol_ratio and vol_ratio < 0.7:
            volume -= 0.4
        volume = max(-1, min(1, volume))

        # structure: last candle and break of structure
        structure = 0.0
        last_dir = ta.last_candle_direction(candles)
        if last_dir == "BULLISH":
            structure += 0.7
        elif last_dir == "BEARISH":
            structure -= 0.7
        if ta.breaks_structure(candles, "LONG"):
            structure += 0.6
        elif ta.breaks_structure(candles, "SHORT"):
            structure -= 0.6
        structure = max(-1, min(1, structure))

        # volatility: too high is a risk, too low = weak signal
        vol_score = 0.0
        if atr_pct <= 0:
            vol_score = 0
        elif atr_pct > self.settings.max_atr_pct:
            vol_score = -1
        elif atr_pct < self.settings.min_atr_pct:
            vol_score = -0.5
        elif atr_pct <= 4:
            vol_score = 0.5
        elif atr_pct <= 8:
            vol_score = 0.2

        score = trend * 1.0 + momentum * 0.9 + volume * 0.6 + structure * 0.75 + vol_score * 0.4
        conviction = math.tanh(score / 3.0)
        return FactorSnapshot(
            trend=trend,
            momentum=momentum,
            volume=volume,
            structure=structure,
            volatility=vol_score,
            score=score,
            conviction=conviction,
        )

    def _agreement(
        self, factors: dict[str, FactorSnapshot], direction: str
    ) -> tuple[float, float]:
        sign = 1.0 if direction == "LONG" else -1.0
        agree = 0.0
        total = 0
        for snap in factors.values():
            values = [snap.trend, snap.momentum, snap.structure]
            agree += sign * (snap.trend + snap.momentum + snap.structure)
            total += len(values)
        if total == 0:
            return 0.0, 0.0
        # normalize to -1..1
        agreement = agree / (total)
        return max(-1.0, min(1.0, agreement)), sum(f.conviction * sign for f in factors.values()) / max(len(factors), 1)

    def _plan(self, data: MarketData, direction: str) -> Optional[dict]:
        # Use the most granular available candles for ATR to respect real micro-structure.
        candles: list[Candle] = []
        for tf in TIMEFRAMES_SCALP + TIMEFRAMES_ALL:
            if tf in data.candles and len(data.candles[tf]) >= 20:
                candles = data.candles[tf]
                break
        if not candles:
            return None
        price = data.price if data.price > 0 else candles[-1].close
        atr_v = ta.atr(candles, 14) or price * 0.02
        atr_pct = atr_v / price * 100.0 if price else 0.0
        atr_v = max(atr_v, price * 0.001)

        if direction == "LONG":
            entry_low = price - 0.4 * atr_v
            entry_high = price + 0.15 * atr_v
            stop = entry_low - 1.2 * atr_v
        else:
            entry_low = price - 0.15 * atr_v
            entry_high = price + 0.4 * atr_v
            stop = entry_high + 1.2 * atr_v

        if stop <= 0:
            return None

        # Risk reward 1.5 / 2.5 / 4.0
        if direction == "LONG":
            risk = entry_low - stop
            tp1 = entry_high + risk * 1.5
            tp2 = entry_high + risk * 2.5
            tp3 = entry_high + risk * 4.0
        else:
            risk = stop - entry_high
            tp1 = entry_high - risk * 1.5
            tp2 = entry_high - risk * 2.5
            tp3 = entry_high - risk * 4.0
        if risk <= 0:
            return None
        rr = [1.5, 2.5, 4.0]
        return {
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "rr": rr,
            "atr_pct": atr_pct,
        }

    def _rationale(
        self,
        data: MarketData,
        factors: dict[str, FactorSnapshot],
        direction: str,
        der_extra: Iterable[str],
    ) -> str:
        best_tf = self._best_tf(factors)
        snap = factors.get(best_tf)
        closes = [c.close for c in data.candles.get(best_tf, [])]
        price = data.price if data.price else (closes[-1] if closes else 0)
        rsi_v = ta.rsi(closes, 14)
        ema20 = ta.ema(closes, 20)
        ema50 = ta.ema(closes, 50)
        vol_ratio = ta.volume_ratio(data.candles.get(best_tf, []), 20)
        change = data.ticker.change_24h_pct if data.ticker else 0.0
        der = data.derivatives

        parts: list[str] = []
        side = "покупатели" if direction == "LONG" else "продавцы"
        if snap is not None:
            if snap.trend > 0.35:
                parts.append(f"структура на {best_tf} бычья (EMA20>EMA50, восходящий наклон)")
            elif snap.trend < -0.35:
                parts.append(f"структура на {best_tf} медвежья (EMA20<EMA50, нисходящий наклон)")
            else:
                parts.append(f"структура на {best_tf} нейтральная, есть неопределённость")
        if rsi_v is not None:
            if direction == "LONG" and 45 <= rsi_v <= 70:
                parts.append(f"RSI {rsi_v:.1f} даёт пространство для продолжения")
            elif direction == "SHORT" and 30 <= rsi_v <= 55:
                parts.append(f"RSI {rsi_v:.1f} подтверждает давление продавцов")
            elif rsi_v > 80 or rsi_v < 20:
                parts.append(f"RSI {rsi_v:.1f} — экстремум, возможен разворот")
            else:
                parts.append(f"RSI {rsi_v:.1f}")
        if ema20 and ema50:
            if direction == "LONG" and ema20 > ema50:
                parts.append(f"EMA20({ema20:.5g}) выше EMA50({ema50:.5g})")
            elif direction == "SHORT" and ema20 < ema50:
                parts.append(f"EMA20({ema20:.5g}) ниже EMA50({ema50:.5g})")
        if vol_ratio is not None:
            kind = "на объёме" if vol_ratio >= 1.2 else "на слабом объёме"
            parts.append(f"последний бар x{vol_ratio:.2f} среднего объёма — движение {kind}")
        if change:
            parts.append(f"изменение за 24h: {change:+.2f}% (Binance spot)")
        if data.btc_change_24h_pct:
            corr_text = f"корреляция с BTC {data.btc_corr_returns:+.2f}" if abs(data.btc_corr_returns) > 0.2 else "корреляция с BTC низкая"
            parts.append(corr_text)
        if der:
            if der.funding_rate is not None:
                parts.append(f"funding {der.funding_rate * 100:+.4f}%/8h")
            if der.open_interest_value_usd is not None:
                parts.append(f"OI ~${der.open_interest_value_usd:,.0f}")
            if der.open_interest is not None and der.open_interest_value_usd is None:
                parts.append(f"OI ~{der.open_interest:.0f} контрактов")
            if der.liquidation_24h_usd is not None:
                parts.append(f"ликвидации 24h ~${der.liquidation_24h_usd:,.0f}")
        parts.extend(der_extra)
        if not parts:
            parts.append("данные только со свечных сессий; производные и новости недоступны")
        body = "; ".join(parts[:6])
        return f"{'Покупка' if direction == 'LONG' else 'Продажа'} на основе реальных данных. {body}. Текущий приоритет — {side}."

    def _risks(self, data: MarketData, direction: str, atr_pct: float) -> str:
        risks: list[str] = []
        if atr_pct >= 6:
            risks.append(f"высокая волатильность (ATR {atr_pct:.2f}%)")
        elif atr_pct >= 3:
            risks.append(f"повышенная волатильность (ATR {atr_pct:.2f}%)")
        der = data.derivatives
        if der and der.funding_rate is not None and abs(der.funding_rate) > 0.0012:
            risks.append(f"переполненный funding ({der.funding_rate * 100:+.4f}%/8h)")
        if abs(data.btc_corr_returns) > 0.75 and data.btc_change_24h_pct:
            risks.append(f"BTC движется {data.btc_change_24h_pct:+.2f}% — сильная корреляция")
        if data.news:
            risks.append(f"свежие новости: {data.news[0].get('title', '')[:60]}")
        r = "; ".join(risks) if risks else "проскальзывание, разворот тренда, резкий выход объёма"
        return f"{r}. Управляй размером позиции."

    def _timeframe_weights(self, tfs: Iterable[str], mode: str) -> dict[str, float]:
        tfs = list(tfs)
        if mode == "scalp":
            w = {tf: 1.0 for tf in tfs}
            if "15m" in tfs:
                w["15m"] = 1.6
            if "1h" in tfs:
                w["1h"] = 1.2
            if "4h" in tfs:
                w["4h"] = 0.6
            if "1d" in tfs:
                w["1d"] = 0.4
            return w
        if mode == "swing":
            w = {tf: 1.0 for tf in tfs}
            if "4h" in tfs:
                w["4h"] = 1.5
            if "1d" in tfs:
                w["1d"] = 1.9
            if "1h" in tfs:
                w["1h"] = 0.7
            if "15m" in tfs:
                w["15m"] = 0.4
            return w
        return {tf: 1.0 for tf in tfs}

    def _best_tf(self, factors: dict[str, FactorSnapshot]) -> str:
        if not factors:
            return "1h"
        return sorted(factors.items(), key=lambda kv: abs(kv[1].conviction), reverse=True)[0][0]

    def _display_timeframe(self, factors: dict[str, FactorSnapshot], mode: str) -> str:
        tf = self._best_tf(factors)
        if mode == "scalp" and tf not in ("15m", "1h"):
            return "1h"
        if mode == "swing" and tf not in ("4h", "1d"):
            return "4h"
        return tf

    def _threshold(self, risk_profile: str) -> int:
        return self.settings.risk_thresholds.get(risk_profile, self.settings.min_confidence)


class Scanner:
    """Scans the real top-volatility universe and returns high-quality setups."""

    def __init__(self, settings: Settings, market: MarketService, engine: SignalEngine) -> None:
        self.settings = settings
        self.market = market
        self.engine = engine

    async def scan(
        self,
        mode: str = "best",
        direction: Optional[str] = None,
        limit: int = 3,
        risk_profile: str = "balanced",
        min_volume_usd: Optional[float] = None,
        min_abs_change: Optional[float] = None,
        max_atr: Optional[float] = None,
    ) -> list[Signal]:
        # Choose the timeframes that match the requested mode.
        if mode == "scalp":
            tfs = TIMEFRAMES_SCALP
        elif mode == "swing":
            tfs = TIMEFRAMES_SWING
        else:
            tfs = TIMEFRAMES_FALLBACK

        tickers = await self.market.top_tickers(self.settings.top_n_symbols)
        if not tickers:
            return []
        if min_volume_usd:
            tickers = [t for t in tickers if t.volume_24h_quote >= min_volume_usd]
        if min_abs_change:
            tickers = [t for t in tickers if abs(t.change_24h_pct) >= min_abs_change]

        # Pre-filter with real 24h metrics only, then validate with candles.
        ranked = self._rank_tickers(tickers, direction)
        candidates = ranked[:35]
        selected: list[MarketData] = []

        sem = asyncio.Semaphore(8)
        async def _load(ticker: TickerInfo) -> Optional[MarketData]:
            async with sem:
                try:
                    return await self.market.get_market_data(
                        ticker.symbol,
                        timeframes=tfs,
                        include_derivatives=False,
                        include_news=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("scan skip {}: {}", ticker.symbol, exc)
                    return None

        datasets = await asyncio.gather(*[_load(t) for t in candidates])
        for data in datasets:
            if data is None:
                continue
            try:
                sig = self.engine.build_signal(
                    data,
                    mode=mode,
                    direction=direction,
                    risk_profile=risk_profile,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("signal error {}: {}", data.symbol, exc)
                continue
            if sig is not None and (max_atr is None or sig.atr_pct <= max_atr):
                selected.append((sig))
        selected.sort(key=lambda s: (s.confidence, abs(s.rr3)), reverse=True)
        return selected[: min(limit, self.settings.top_scans)]

    def _rank_tickers(self, tickers: list[TickerInfo], direction: Optional[str]) -> list[TickerInfo]:
        active = [t for t in tickers if t.last_price > 0 and t.volume_24h_quote > 0]
        if direction == "LONG":
            active = [t for t in active if t.change_24h_pct >= 0]
        elif direction == "SHORT":
            active = [t for t in active if t.change_24h_pct <= 0]

        # Combine liquidity with directional momentum / volatility.
        def score(t: TickerInfo) -> float:
            vol = math.log10(max(t.volume_24h_quote, 1.0)) * 1.0
            mom = abs(t.change_24h_pct) * 1.2
            return vol + mom

        active.sort(key=score, reverse=True)
        return active
