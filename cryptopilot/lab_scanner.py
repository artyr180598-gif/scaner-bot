from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from cryptopilot.config import Settings
from cryptopilot.exchange import ExchangeClient
from cryptopilot.indicators import compute_features, directional_score, InsufficientData
from cryptopilot.hummingbot_fusion import evaluate as evaluate_hummingbot_fusion
from cryptopilot.models import Candle, Side, Ticker


@dataclass(frozen=True, slots=True)
class LabCandidate:
    symbol: str
    side: Side
    stage: str
    price: float
    score: float
    confidence: int
    regime: str
    entry_low: float
    entry_high: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    rr: float
    leverage: int
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    indicators: tuple[str, ...]
    backtest: object | None = None


class LabMarketScanner:
    """Independent research scanner for Hummingbot Lab.

    It deliberately does not call MarketScanner.scan_market(), PRIME or RID.
    It uses the same exchange data source but its own ranking and entry logic.
    """

    def __init__(self, exchange: ExchangeClient, settings: Settings) -> None:
        self.exchange = exchange
        self.settings = settings
        self._concurrency = max(2, min(settings.request_concurrency, 10))

    async def scan(self, limit: int = 5) -> tuple[int, int, list[LabCandidate]]:
        tickers = await self.exchange.tickers()
        universe = [
            t for t in tickers
            if t.symbol.endswith("USDT")
            and t.symbol not in self.settings.excluded_symbol_set
            and t.turnover_24h >= min(self.settings.min_volume_usdt, 5_000_000)
            and t.spread_bps <= max(self.settings.max_spread_bps, 20)
        ]
        universe.sort(key=lambda x: x.turnover_24h, reverse=True)
        universe = universe[: self.settings.universe_size]

        sem = asyncio.Semaphore(self._concurrency)

        async def quick(ticker: Ticker):
            async with sem:
                try:
                    candles = await self.exchange.candles(ticker.symbol, "15", 230)
                    f = compute_features(candles)
                    score = self._discovery_score(f, ticker)
                    return score, ticker, candles
                except (Exception,):
                    return None

        raw = await asyncio.gather(*(quick(t) for t in universe))
        ranked = sorted((x for x in raw if x is not None), key=lambda x: x[0], reverse=True)
        shortlist = ranked[: max(12, min(24, self.settings.shortlist_size * 2))]

        btc = []
        try:
            btc = await self.exchange.candles("BTCUSDT", "60", 230)
        except Exception:
            pass
        btc_feature = None
        if btc:
            try:
                btc_feature = compute_features(btc)
            except Exception:
                pass

        async def deep(item):
            _, ticker, c15 = item
            async with sem:
                try:
                    c60, c240 = await asyncio.gather(
                        self.exchange.candles(ticker.symbol, "60", 230),
                        self.exchange.candles(ticker.symbol, "240", 230),
                    )
                    return self._candidate(ticker, c15, c60, c240, btc_feature)
                except Exception:
                    return None

        deep_results = await asyncio.gather(*(deep(x) for x in shortlist))
        candidates = [x for x in deep_results if x is not None]
        candidates.sort(key=lambda x: (x.score, x.confidence), reverse=True)
        return len(universe), len(shortlist), candidates[: max(1, limit)]

    @staticmethod
    def _discovery_score(f, ticker: Ticker) -> float:
        direction = abs(directional_score(f))
        compression = 0
        compression += 12 if f.keltner_squeeze_ratio < 1.0 else 0
        compression += 8 if f.bb_width_regime_ratio < 0.9 else 0
        compression += 6 if f.atr_regime_ratio < 0.9 else 0
        early = max(0.0, 12.0 - abs(f.range_position20 - (0.75 if direction > 0 else 0.25)) * 16)
        flow = min(12.0, max(0.0, f.relative_volume20 * 5))
        liquidity = 8 if ticker.turnover_24h >= 20_000_000 else 4
        late_penalty = 18 if f.breakout_up or f.breakout_down else 0
        return max(0.0, direction * 0.55 + compression + early + flow + liquidity - late_penalty)

    def _candidate(self, ticker: Ticker, c15: list[Candle], c60: list[Candle],
                   c240: list[Candle], btc_feature) -> LabCandidate | None:
        f15, f60, f240 = compute_features(c15), compute_features(c60), compute_features(c240)
        s15, s60, s240 = directional_score(f15), directional_score(f60), directional_score(f240)

        # Direction comes from a weighted multi-timeframe ensemble, not PRIME/RID.
        raw = s15 * 0.30 + s60 * 0.45 + s240 * 0.25
        side = Side.LONG if raw > 0 else Side.SHORT
        if abs(raw) < 8:
            return None

        reasons: list[str] = []
        risks: list[str] = []
        points = 0.0

        # Fuse Hummingbot-style trades/order-book/execution data into the
        # independent Lab ranking. This is confirmation, not a standalone
        # entry trigger.
        hb = evaluate_hummingbot_fusion(side, ticker)
        points += hb.score_delta
        reasons.extend(hb.reasons)
        risks.extend(hb.risks)

        aligned = (s60 > 0 and s240 > 0) if side is Side.LONG else (s60 < 0 and s240 < 0)
        if aligned:
            points += 18
            reasons.append("EMA/Trend aligned on 1h + 4h")
        else:
            risks.append("1h/4h trend not fully aligned")

        momentum_ok = (
            (f60.rsi14 >= 52 and f60.macd_hist > 0)
            if side is Side.LONG
            else (f60.rsi14 <= 48 and f60.macd_hist < 0)
        )
        if momentum_ok:
            points += 12
            reasons.append(f"RSI {f60.rsi14:.0f} + MACD support direction")

        if f60.keltner_squeeze_ratio < 1.0 and f60.squeeze_bars >= 2:
            points += 15
            reasons.append(f"pre-breakout squeeze {f60.squeeze_bars} bars")
        elif f60.bb_width_regime_ratio < 0.9:
            points += 8
            reasons.append("Bollinger compression")

        if f15.relative_volume20 > 1.15:
            points += min(10, (f15.relative_volume20 - 1.0) * 20)
            reasons.append(f"relative volume {f15.relative_volume20:.2f}x")

        if f60.cmf20 > 0.08 and side is Side.LONG:
            points += 8
            reasons.append(f"CMF accumulation {f60.cmf20:+.2f}")
        elif f60.cmf20 < -0.08 and side is Side.SHORT:
            points += 8
            reasons.append(f"CMF distribution {f60.cmf20:+.2f}")

        extra = self._advanced(c15, c60, side)
        points += extra["points"]
        reasons.extend(extra["reasons"])
        risks.extend(extra["risks"])

        if ticker.open_interest_change_pct is not None:
            oi = ticker.open_interest_change_pct
            if (side is Side.LONG and oi > 0.8) or (side is Side.SHORT and oi > 0.8):
                points += min(8, 3 + oi)
                reasons.append(f"open interest +{oi:.1f}%")
            elif oi < -3:
                points -= 5
                risks.append("open interest falling")

        if ticker.taker_buy_ratio is not None:
            aligned_flow = (
                ticker.taker_buy_ratio >= 0.54
                if side is Side.LONG
                else ticker.taker_buy_ratio <= 0.46
            )
            if aligned_flow:
                points += 7
                reasons.append(f"taker flow {ticker.taker_buy_ratio:.0%} aligned")
            else:
                risks.append(f"taker flow {ticker.taker_buy_ratio:.0%} not aligned")

        if btc_feature is not None:
            btc_dir = directional_score(btc_feature)
            if (side is Side.LONG and btc_dir > 20) or (side is Side.SHORT and btc_dir < -20):
                points += 6
                reasons.append("BTC regime supports direction")
            elif (side is Side.LONG and btc_dir < -35) or (side is Side.SHORT and btc_dir > 35):
                points -= 8
                risks.append("BTC regime opposes direction")

        # Do not reward an already explosive move. Lab is intended to find earlier entries.
        if f15.breakout_up or f15.breakout_down:
            points -= 16
            risks.append("15m breakout already active — entry may be late")
        if abs(f15.vwap_distance_atr) > 1.8:
            points -= 8
            risks.append("price stretched from VWAP")

        score = float(np.clip(45 + points + min(18, abs(raw) * 0.18), 0, 100))
        confidence = int(np.clip(score, 50, 95))
        stage = "EARLY WATCH"
        if score >= 78 and aligned and not (f15.breakout_up or f15.breakout_down):
            stage = "ENTRY SETUP"
        elif score >= 68:
            stage = "PREPARE"

        price = ticker.last
        atr = max(f15.atr14, price * 0.002)
        if side is Side.LONG:
            entry_low = max(f15.ema20 - 0.35 * atr, price - 0.30 * atr)
            entry_high = min(price + 0.10 * atr, f60.range_high20)
            stop = min(f15.range_low20, f60.range_low20, f15.ema50 - 0.5 * atr)
            if stop >= entry_low:
                stop = price - 1.5 * atr
            risk = max(entry_high - stop, 0.5 * atr)
            tp1, tp2, tp3 = entry_high + risk, entry_high + 2 * risk, entry_high + 3 * risk
        else:
            entry_low = max(price - 0.10 * atr, f60.range_low20)
            entry_high = min(f15.ema20 + 0.35 * atr, price + 0.30 * atr)
            stop = max(f15.range_high20, f60.range_high20, f15.ema50 + 0.5 * atr)
            if stop <= entry_high:
                stop = price + 1.5 * atr
            risk = max(stop - entry_low, 0.5 * atr)
            tp1, tp2, tp3 = entry_low - risk, entry_low - 2 * risk, entry_low - 3 * risk

        stop_pct = abs(price - stop) / max(price, 1e-12) * 100
        if stop_pct > self.settings.hummingbot_lab_max_stop_pct * 100:
            risks.append(f"stop distance {stop_pct:.1f}% is wide")
            score = max(0, score - 7)
        rr = (
            abs(tp2 - entry_high) / max(abs(entry_high - stop), 1e-12)
            if side is Side.LONG
            else abs(entry_low - tp2) / max(abs(stop - entry_low), 1e-12)
        )
        leverage = 1 if score < 78 else min(2, self.settings.max_leverage)

        indicators = (
            f"EMA20/50/200 {f15.ema20:.6g}/{f15.ema50:.6g}/{f15.ema200:.6g}",
            f"RSI14 {f60.rsi14:.1f}",
            f"ADX14 {f60.adx14:.1f}",
            f"ATR {f15.atr_pct:.2f}%",
            f"BB {f60.bb_width_regime_ratio:.2f}x",
            f"KC squeeze {f60.keltner_squeeze_ratio:.2f}",
            f"CMF20 {f60.cmf20:+.2f}",
            f"RVOL20 {f15.relative_volume20:.2f}x",
            f"Supertrend 1h {'UP' if f60.supertrend_direction > 0 else 'DOWN'}",
        )
        return LabCandidate(
            symbol=ticker.symbol, side=side, stage=stage, price=price,
            score=round(score, 1), confidence=confidence,
            regime=self._regime(btc_feature),
            entry_low=min(entry_low, entry_high), entry_high=max(entry_low, entry_high),
            stop=stop, tp1=tp1, tp2=tp2, tp3=tp3,
            rr=rr, leverage=leverage,
            reasons=tuple(reasons[:7]), risks=tuple(risks[:5]),
            indicators=indicators,
        )

    @staticmethod
    def _regime(f) -> str:
        if f is None:
            return "UNKNOWN"
        score = directional_score(f)
        if score >= 30:
            return "BULL"
        if score <= -30:
            return "BEAR"
        return "TRANSITION"

    @staticmethod
    def _advanced(c15: list[Candle], c60: list[Candle], side: Side) -> dict:
        close = np.array([c.close for c in c60], dtype=float)
        high = np.array([c.high for c in c60], dtype=float)
        low = np.array([c.low for c in c60], dtype=float)
        volume = np.array([c.volume for c in c60], dtype=float)
        points = 0.0
        reasons: list[str] = []
        risks: list[str] = []

        # Stoch RSI
        delta = np.diff(close, prepend=close[0])
        gains = np.maximum(delta, 0)
        losses = np.maximum(-delta, 0)
        ag = _ema(gains, 14)
        al = _ema(losses, 14)
        rsi = 100 - 100 / (1 + np.divide(ag, al, out=np.full_like(ag, 100.0), where=al > 1e-12))
        lo = np.min(rsi[-14:])
        hi = np.max(rsi[-14:])
        stoch_rsi = (rsi[-1] - lo) / max(hi - lo, 1e-12)
        if side is Side.LONG and 0.20 <= stoch_rsi <= 0.80:
            points += 4
            reasons.append(f"StochRSI {stoch_rsi:.2f} not overheated")
        elif side is Side.SHORT and 0.20 <= stoch_rsi <= 0.80:
            points += 4
            reasons.append(f"StochRSI {stoch_rsi:.2f} not exhausted")

        # MFI
        typical = (high + low + close) / 3
        raw_flow = typical * volume
        pos = np.where(np.diff(typical, prepend=typical[0]) > 0, raw_flow, 0)
        neg = np.where(np.diff(typical, prepend=typical[0]) < 0, raw_flow, 0)
        mfi_ratio = np.sum(pos[-14:]) / max(np.sum(neg[-14:]), 1e-12)
        mfi = 100 - 100 / (1 + mfi_ratio)
        if (side is Side.LONG and mfi > 52) or (side is Side.SHORT and mfi < 48):
            points += 5
            reasons.append(f"MFI14 {mfi:.0f} confirms flow")

        # OBV slope / accumulation
        obv = np.cumsum(np.where(np.diff(close, prepend=close[0]) >= 0, volume, -volume))
        obv_slope = (obv[-1] - obv[-10]) / max(np.mean(volume[-30:]) * 10, 1e-12)
        if (side is Side.LONG and obv_slope > 0.15) or (side is Side.SHORT and obv_slope < -0.15):
            points += 5
            reasons.append(f"OBV flow {'up' if obv_slope > 0 else 'down'}")

        # PSAR-like trend check using a conservative close-confirmed trailing extreme.
        psar_up = close[-1] > np.min(low[-8:])
        if (side is Side.LONG and psar_up) or (side is Side.SHORT and not psar_up):
            points += 4
            reasons.append("PSAR/trailing trend agrees")

        # Donchian structure + early position
        dh, dl = np.max(high[-20:-1]), np.min(low[-20:-1])
        pos20 = (close[-1] - dl) / max(dh - dl, 1e-12)
        if (
            (side is Side.LONG and 0.60 <= pos20 <= 0.90)
            or (side is Side.SHORT and 0.10 <= pos20 <= 0.40)
        ):
            points += 5
            reasons.append(f"Donchian position {pos20:.2f} leaves room for expansion")

        # FVG and liquidity sweep, using only closed candles.
        if len(c60) >= 4:
            a, b, c = c60[-3], c60[-2], c60[-1]
            bullish_fvg = c.low > a.high
            bearish_fvg = c.high < a.low
            if (side is Side.LONG and bullish_fvg) or (side is Side.SHORT and bearish_fvg):
                points += 4
                reasons.append("recent FVG supports direction")
            swept_low = b.low < min(x.low for x in c60[-12:-2]) and c.close > b.low
            swept_high = b.high > max(x.high for x in c60[-12:-2]) and c.close < b.high
            if (side is Side.LONG and swept_low) or (side is Side.SHORT and swept_high):
                points += 6
                reasons.append("liquidity sweep/reclaim detected")
        return {"points": points, "reasons": reasons, "risks": risks}


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    result = np.empty_like(values, dtype=float)
    result[0] = values[0]
    alpha = 2 / (period + 1)
    for i in range(1, len(values)):
        result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]
    return result
