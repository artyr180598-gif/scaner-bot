from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptopilot.config import Settings
from cryptopilot.exchange import ExchangeClient
from cryptopilot.indicators import compute_features
from cryptopilot.models import FeatureSet, Side, Ticker


@dataclass(frozen=True, slots=True)
class SmartMoneySetup:
    symbol: str
    exchange: str
    bias: Side
    score: int
    stage: str
    price: float
    trigger_price: float
    invalidation_price: float
    structure_15m: str
    structure_1h: str
    rvol: float
    oi_change_pct: float | None
    taker_buy_ratio: float | None
    orderbook_imbalance: float | None
    funding_pct: float
    created_at: datetime
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SmartMoneyReport:
    exchange: str
    started_at: datetime
    finished_at: datetime
    universe_count: int
    analyzed_count: int
    setups: tuple[SmartMoneySetup, ...]
    errors: tuple[str, ...] = ()


class SmartMoneyScanner:
    """Detect price/volume/derivatives confluence without pretending to identify institutions.

    Public exchange data cannot tell us *who* traded. This scanner therefore looks for a
    measurable footprint: market structure + relative volume + open-interest expansion +
    aggressive taker flow + higher-timeframe alignment. Order-book imbalance and funding are
    deliberately low-weight because a snapshot can be spoofed or disappear quickly.
    """

    def __init__(self, exchange: ExchangeClient, settings: Settings) -> None:
        self.exchange = exchange
        self.settings = settings
        self._lock = asyncio.Lock()

    async def scan(self) -> SmartMoneyReport:
        async with self._lock:
            started = datetime.now(UTC)
            errors: list[str] = []
            tickers = await self.exchange.tickers()
            universe = self._universe(tickers)

            quick = await asyncio.gather(
                *(self._quick(item) for item in universe),
                return_exceptions=True,
            )
            ranked: list[tuple[float, Ticker, FeatureSet]] = []
            for ticker, result in zip(universe, quick, strict=True):
                if isinstance(result, BaseException):
                    errors.append(f"{ticker.symbol}: {type(result).__name__}")
                    continue
                feature = result
                ranked.append((self._pre_score(feature), ticker, feature))

            ranked.sort(key=lambda item: item[0], reverse=True)
            deep_limit = min(max(self.settings.shortlist_size, 12), 24)
            candidates = ranked[:deep_limit]

            deep = await asyncio.gather(
                *(self._deep(ticker, feature15) for _, ticker, feature15 in candidates),
                return_exceptions=True,
            )
            setups: list[SmartMoneySetup] = []
            for candidate, result in zip(candidates, deep, strict=True):
                ticker = candidate[1]
                if isinstance(result, BaseException):
                    errors.append(f"{ticker.symbol}: {type(result).__name__}")
                    continue
                if result is not None:
                    setups.append(result)

            stage_rank = {"ENTRY": 3, "ARMED": 2, "WATCH": 1}
            setups.sort(key=lambda item: (stage_rank.get(item.stage, 0), item.score), reverse=True)
            return SmartMoneyReport(
                exchange=self.exchange.name,
                started_at=started,
                finished_at=datetime.now(UTC),
                universe_count=len(universe),
                analyzed_count=len(candidates),
                setups=tuple(setups[:8]),
                errors=tuple(errors[:12]),
            )

    def _universe(self, tickers: list[Ticker]) -> list[Ticker]:
        values = [
            item
            for item in tickers
            if item.symbol not in self.settings.excluded_symbol_set
            and item.turnover_24h >= self.settings.min_volume_usdt
            and item.last > 0
            and item.bid > 0
            and item.ask >= item.bid
            and item.spread_bps <= self.settings.max_spread_bps
        ]
        values.sort(key=lambda item: item.turnover_24h, reverse=True)
        return values[: self.settings.universe_size]

    async def _quick(self, ticker: Ticker) -> FeatureSet:
        candles = await self.exchange.candles(ticker.symbol, "15", 240)
        return compute_features(candles)

    @staticmethod
    def _pre_score(feature: FeatureSet) -> float:
        structure = max(
            _structure_score(feature, Side.LONG),
            _structure_score(feature, Side.SHORT),
        )
        breakout_proximity = max(feature.range_position20, 1 - feature.range_position20)
        return (
            structure
            + min(feature.relative_volume20, 3.0) * 7
            + breakout_proximity * 8
            + max(0.0, 1.2 - feature.keltner_squeeze_ratio) * 6
        )

    async def _deep(self, ticker: Ticker, feature15: FeatureSet) -> SmartMoneySetup | None:
        enriched, candles_1h = await asyncio.gather(
            self.exchange.enrich_ticker(ticker),
            self.exchange.candles(ticker.symbol, "60", 240),
        )
        feature1h = compute_features(candles_1h)

        long_score, long_reasons, long_warnings = _direction_score(
            Side.LONG, feature15, feature1h, enriched
        )
        short_score, short_reasons, short_warnings = _direction_score(
            Side.SHORT, feature15, feature1h, enriched
        )
        if max(long_score, short_score) < 62:
            return None

        bias = Side.LONG if long_score >= short_score else Side.SHORT
        score = int(round(max(long_score, short_score)))
        reasons = long_reasons if bias is Side.LONG else short_reasons
        warnings = long_warnings if bias is Side.LONG else short_warnings
        stage = _stage(bias, score, feature15, feature1h, enriched)

        if stage == "WATCH" and score < 65:
            return None

        trigger = feature15.range_high20 if bias is Side.LONG else feature15.range_low20
        invalidation = feature15.range_low20 if bias is Side.LONG else feature15.range_high20
        return SmartMoneySetup(
            symbol=ticker.symbol,
            exchange=self.exchange.name,
            bias=bias,
            score=score,
            stage=stage,
            price=feature15.close,
            trigger_price=trigger,
            invalidation_price=invalidation,
            structure_15m=_structure_label(feature15),
            structure_1h=_structure_label(feature1h),
            rvol=feature15.relative_volume20,
            oi_change_pct=enriched.open_interest_change_pct,
            taker_buy_ratio=enriched.taker_buy_ratio,
            orderbook_imbalance=enriched.orderbook_imbalance,
            funding_pct=enriched.funding_rate * 100,
            created_at=datetime.now(UTC),
            reasons=tuple(reasons[:5]),
            warnings=tuple(warnings[:4]),
        )


def _structure_score(feature: FeatureSet, side: Side) -> float:
    bullish = side is Side.LONG
    score = 0.0
    aligned = (
        feature.close > feature.ema20 > feature.ema50
        if bullish
        else feature.close < feature.ema20 < feature.ema50
    )
    if aligned:
        score += 10
    if (feature.breakout_up if bullish else feature.breakout_down):
        score += 15
    dmi_ok = feature.dmi_spread >= 8 if bullish else feature.dmi_spread <= -8
    if dmi_ok:
        score += 5
    slope_ok = feature.ema20_slope_pct > 0 if bullish else feature.ema20_slope_pct < 0
    if slope_ok:
        score += 5
    return score


def _direction_score(
    side: Side,
    f15: FeatureSet,
    f1h: FeatureSet,
    ticker: Ticker,
) -> tuple[float, list[str], list[str]]:
    bullish = side is Side.LONG
    score = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    structure = _structure_score(f15, side)
    score += min(structure, 25)
    if structure >= 20:
        reasons.append("15m: подтверждённая структура и пробой/давление к пробою")
    elif structure >= 10:
        reasons.append("15m: структура уже направлена в сторону сценария")

    htf_aligned = (
        f1h.close > f1h.ema50 > f1h.ema200 and f1h.supertrend_direction > 0
        if bullish
        else f1h.close < f1h.ema50 < f1h.ema200 and f1h.supertrend_direction < 0
    )
    if htf_aligned:
        score += 10
        reasons.append("1h: старший тренд подтверждает направление")

    rvol = f15.relative_volume20
    if rvol >= 2:
        volume_points = 15
    elif rvol >= 1.5:
        volume_points = 12
    elif rvol >= 1.2:
        volume_points = 8
    elif rvol >= 1:
        volume_points = 4
    else:
        volume_points = 0
    score += volume_points
    if volume_points >= 8:
        reasons.append(f"RVOL {rvol:.2f}×: объём выше своей 20-свечной нормы")
    elif rvol < 0.8:
        warnings.append("Объём ниже нормы — движение может быть хрупким")

    price_direction = f15.return_20_pct > 0 if bullish else f15.return_20_pct < 0
    oi = ticker.open_interest_change_pct
    if oi is not None:
        if oi > 0 and price_direction:
            oi_points = min(15.0, 5.0 + oi * 1.5)
            score += oi_points
            reasons.append(f"OI {oi:+.1f}%: вместе с ценой растёт число открытых позиций")
        elif oi < -1 and price_direction:
            warnings.append(
                f"OI {oi:+.1f}%: движение может быть закрытием позиций, а не новым входом"
            )

    taker = ticker.taker_buy_ratio
    if taker is not None:
        directional = taker if bullish else 1 - taker
        if directional >= 0.62:
            score += 15
            reasons.append(f"Агрессивный taker-flow {directional:.0%} в сторону {side.value}")
        elif directional >= 0.56:
            score += 10
            reasons.append(f"Taker-flow {directional:.0%} поддерживает {side.value}")
        elif directional <= 0.44:
            score -= 8
            warnings.append("Taker-flow против выбранного направления")

    book = ticker.orderbook_imbalance
    if book is not None:
        directional_book = book if bullish else -book
        if directional_book >= 0.18:
            score += 5
        elif directional_book <= -0.18:
            score -= 3
            warnings.append("Снимок стакана сейчас против направления")

    position = f15.range_position20 if bullish else 1 - f15.range_position20
    if position >= 0.82:
        score += 6
        reasons.append("Цена прижата к границе 20-свечного диапазона")
    if f15.keltner_squeeze_ratio <= 1.05:
        score += 4
        reasons.append("Есть сжатие волатильности перед потенциальным расширением")

    funding_pct = ticker.funding_rate * 100
    if bullish and funding_pct > 0.08:
        score -= 5
        warnings.append(f"Funding {funding_pct:+.3f}%: LONG-сторона перегрета")
    elif not bullish and funding_pct < -0.08:
        score -= 5
        warnings.append(f"Funding {funding_pct:+.3f}%: SHORT-сторона перегрета")
    else:
        score += 3

    if f15.vwap_distance_atr > 2.5 and bullish:
        score -= 7
        warnings.append("Цена слишком далеко выше VWAP — риск погони за импульсом")
    elif f15.vwap_distance_atr < -2.5 and not bullish:
        score -= 7
        warnings.append("Цена слишком далеко ниже VWAP — риск погони за импульсом")

    return max(0.0, min(score, 100.0)), reasons, warnings


def _stage(
    side: Side,
    score: int,
    f15: FeatureSet,
    f1h: FeatureSet,
    ticker: Ticker,
) -> str:
    bullish = side is Side.LONG
    breakout = f15.breakout_up if bullish else f15.breakout_down
    near_level = f15.range_position20 >= 0.82 if bullish else f15.range_position20 <= 0.18
    htf = (
        f1h.supertrend_direction > 0 and f1h.close > f1h.ema50
        if bullish
        else f1h.supertrend_direction < 0 and f1h.close < f1h.ema50
    )
    if ticker.taker_buy_ratio is None:
        taker_ok = True
    elif bullish:
        taker_ok = ticker.taker_buy_ratio >= 0.54
    else:
        taker_ok = ticker.taker_buy_ratio <= 0.46
    oi_ok = ticker.open_interest_change_pct is None or ticker.open_interest_change_pct >= 0
    if (
        score >= 78
        and breakout
        and f15.relative_volume20 >= 1.2
        and taker_ok
        and oi_ok
        and htf
    ):
        return "ENTRY"
    if score >= 72 and near_level and htf:
        return "ARMED"
    return "WATCH"


def _structure_label(feature: FeatureSet) -> str:
    if feature.breakout_up:
        return "BOS ↑"
    if feature.breakout_down:
        return "BOS ↓"
    if feature.close > feature.ema20 > feature.ema50:
        return "HH/HL ↑"
    if feature.close < feature.ema20 < feature.ema50:
        return "LH/LL ↓"
    return "переходная"


def _price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if value >= 0.01:
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_smart_money_report(report: SmartMoneyReport) -> str:
    if not report.setups:
        return (
            "<b>🐋 Smart Money Radar</b>\n"
            f"Проверено: {report.universe_count} · углублённо: {report.analyzed_count}\n\n"
            "Сейчас нет монет, где одновременно совпали структура, объём и деривативный поток. "
            "Это лучше, чем выдавать слабый сигнал."
        )
    entry = sum(item.stage == "ENTRY" for item in report.setups)
    armed = sum(item.stage == "ARMED" for item in report.setups)
    return (
        "<b>🐋 Smart Money Radar</b>\n"
        f"Биржа: {html.escape(report.exchange)} · рынок: {report.universe_count} · "
        f"глубокий анализ: {report.analyzed_count}\n"
        f"ENTRY: {entry} · ARMED: {armed} · показано: {len(report.setups)}\n\n"
        "Это поиск следа крупного потока по публичным данным, а не идентификация конкретных китов."
    )


def format_smart_money_setup(item: SmartMoneySetup) -> str:
    stage_icon = {"ENTRY": "🟢", "ARMED": "🟡", "WATCH": "🔵"}.get(item.stage, "⚪")
    side_icon = "↗️" if item.bias is Side.LONG else "↘️"
    oi = "н/д" if item.oi_change_pct is None else f"{item.oi_change_pct:+.1f}%"
    taker = "н/д" if item.taker_buy_ratio is None else f"{item.taker_buy_ratio:.0%} buy"
    book = "н/д" if item.orderbook_imbalance is None else f"{item.orderbook_imbalance:+.0%}"
    reasons = "\n".join(f"• {html.escape(value)}" for value in item.reasons)
    warnings = "\n".join(f"• {html.escape(value)}" for value in item.warnings)
    stage_text = {
        "ENTRY": "подтверждение уже есть; всё равно не гнаться за свечой",
        "ARMED": "сетап готовится; ждать закрытого 15m подтверждения уровня",
        "WATCH": "наблюдение; вход пока не подтверждён",
    }.get(item.stage, "наблюдение")
    return (
        f"{stage_icon} <b>{html.escape(item.symbol)} · {item.stage} · "
        f"{item.bias.value}</b> {side_icon}\n"
        f"Score: <b>{item.score}/100</b> · цена: <code>{_price(item.price)}</code>\n"
        f"Структура: 15m {item.structure_15m} · 1h {item.structure_1h}\n"
        f"RVOL: {item.rvol:.2f}× · OI: {oi} · taker: {taker}\n"
        f"Book: {book} · funding: {item.funding_pct:+.3f}%\n"
        f"Trigger: <code>{_price(item.trigger_price)}</code> · "
        f"invalidation: <code>{_price(item.invalidation_price)}</code>\n\n"
        f"<b>Почему в списке</b>\n{reasons or '• Совпало несколько независимых факторов'}\n"
        + (f"\n<b>Риски</b>\n{warnings}\n" if warnings else "")
        + f"\n<b>Стадия:</b> {stage_text}.\n"
        "⚠️ Taker-flow здесь — прокси дельты из публичных сделок/агрегатов; "
        "это не биржевой footprint-CVD и не доказательство входа институционала."
    )
