"""Isolated RID-style post-impulse scanner using only closed candles.

The model does not claim to reproduce a private indicator.  It implements the
observable sequence: liquid market -> abnormal impulse -> contracting pullback
-> close-confirmed reactivation.  Every published setup has a structural stop;
averaging never increases the original risk budget.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptopilot.config import Settings
from cryptopilot.economics import net_reward_risk
from cryptopilot.exchange import INTERVAL_MS, ExchangeClient
from cryptopilot.indicators import compute_features
from cryptopilot.models import (
    CURRENT_RID_STRATEGY_VERSION,
    Candle,
    ScanReport,
    Side,
    Signal,
    Ticker,
    TradePlan,
)
from cryptopilot.storage import SignalStore

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImpulseLeg:
    side: Side
    start_index: int
    extreme_index: int
    origin: float
    extreme: float
    size_atr: float
    volume_ratio: float


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _validate_closed_series(bars: list[Candle], interval: str, now: datetime) -> None:
    duration = INTERVAL_MS[interval]
    if len(bars) < 210:
        raise ValueError(f"RID requires 210 closed {interval} candles")
    if any(
        right.open_time_ms - left.open_time_ms != duration
        for left, right in zip(bars, bars[1:], strict=False)
    ):
        raise ValueError(f"RID rejected discontinuous {interval} candles")
    now_ms = int(now.timestamp() * 1000)
    closed_at = bars[-1].open_time_ms + duration
    if closed_at > now_ms:
        raise ValueError(f"RID rejected an unclosed {interval} candle")
    if now_ms - closed_at > 2 * duration + 90_000:
        raise ValueError(f"RID rejected stale {interval} candles")


def _latest_impulse(
    bars: list[Candle],
    atr: float,
    side: Side,
    *,
    min_atr: float = 1.5,
    max_atr: float = 20.0,
    min_rvol: float = 1.2,
) -> ImpulseLeg | None:
    """Return the latest qualifying 3-6 bar displacement before the trigger bar."""
    if len(bars) < 40 or atr <= 0:
        return None
    best: ImpulseLeg | None = None
    first_end = max(25, len(bars) - 30)
    # Keep the final closed candle exclusively for the reactivation check.
    for end in range(first_end, len(bars) - 1):
        start = max(20, end - 5)
        leg = bars[start : end + 1]
        baseline = _median([bar.volume for bar in bars[start - 20 : start]])
        if baseline <= 0:
            continue
        if side is Side.LONG:
            origin_offset = min(range(len(leg) - 1), key=lambda i: leg[i].low)
            extreme_offset = origin_offset + max(
                range(len(leg) - origin_offset),
                key=lambda i: leg[origin_offset + i].high,
            )
            origin = leg[origin_offset].low
            extreme = leg[extreme_offset].high
        else:
            origin_offset = max(range(len(leg) - 1), key=lambda i: leg[i].high)
            extreme_offset = origin_offset + min(
                range(len(leg) - origin_offset),
                key=lambda i: leg[origin_offset + i].low,
            )
            origin = leg[origin_offset].high
            extreme = leg[extreme_offset].low
        extreme_index = start + extreme_offset
        size = abs(extreme - origin)
        if size <= 0:
            continue
        candidate = ImpulseLeg(
            side=side,
            start_index=start,
            extreme_index=extreme_index,
            origin=float(origin),
            extreme=float(extreme),
            size_atr=float(size / atr),
            volume_ratio=float(max(bar.volume for bar in leg) / baseline),
        )
        if not min_atr <= candidate.size_atr <= max_atr:
            continue
        if candidate.volume_ratio < min_rvol:
            continue
        if candidate.extreme_index > len(bars) - 3:
            continue
        # Recency wins after minimum displacement; strength breaks ties.
        if best is None or (candidate.extreme_index, candidate.size_atr) > (
            best.extreme_index,
            best.size_atr,
        ):
            best = candidate
    return best


def _plan(
    side: Side,
    ticker: Ticker,
    bars: list[Candle],
    leg: ImpulseLeg,
    atr: float,
    settings: Settings,
    now: datetime,
) -> tuple[TradePlan | None, list[str]]:
    entry = ticker.ask if side is Side.LONG else ticker.bid
    blockers: list[str] = []
    if not math.isfinite(entry) or entry <= 0 or ticker.bid <= 0 or ticker.ask < ticker.bid:
        return None, ["Свежая исполнимая цена недоступна"]
    if abs(entry - bars[-1].close) > 0.35 * atr:
        return None, ["Цена ушла от последней закрытой свечи — вход уже несвежий"]

    post = bars[leg.extreme_index :]
    if side is Side.LONG:
        swing = min(bar.low for bar in post)
        stop = swing - 0.12 * atr
        risk = entry - stop
    else:
        swing = max(bar.high for bar in post)
        stop = swing + 0.12 * atr
        risk = stop - entry
    if risk <= 0:
        return None, ["Структурный стоп оказался по неверную сторону входа"]
    stop_pct = risk / entry * 100
    if stop_pct < 0.35:
        risk = entry * 0.0035
        stop = entry - risk if side is Side.LONG else entry + risk
        stop_pct = 0.35
    if stop_pct > settings.rid_max_stop_pct:
        return None, [
            f"Структурный стоп {stop_pct:.2f}% слишком широк для RID-плана "
            f"(максимум {settings.rid_max_stop_pct:.2f}%)"
        ]

    # Three planned fills improve the average price without martingale sizing.
    sign = 1 if side is Side.LONG else -1
    first = entry
    second = entry - sign * 0.22 * risk
    third = entry - sign * 0.44 * risk
    worst_entry = max(first, second, third) if side is Side.LONG else min(first, second, third)
    worst_risk = sign * (worst_entry - stop)
    # Targets remain valid even when only the least favourable first fill executes.
    tp1 = worst_entry + sign * worst_risk
    tp2 = worst_entry + sign * 2.10 * worst_risk
    tp3 = worst_entry + sign * 3.0 * worst_risk
    rr = net_reward_risk(
        side is Side.LONG,
        worst_entry,
        stop,
        tp2,
        settings.paper_one_way_cost_bps,
    )
    if rr < settings.rid_min_plan_rr:
        return None, [
            f"TP2 даёт только {rr:.2f}R после издержек; нужно {settings.rid_min_plan_rr:.2f}R+"
        ]

    risk_budget = settings.account_equity_usdt * settings.risk_per_trade_pct / 100
    cost = settings.paper_one_way_cost_bps / 10_000
    loss_per_unit = abs(worst_entry - stop) + (worst_entry + stop) * cost
    leverage = min(settings.preferred_leverage, 2, settings.max_leverage)
    position_cap = settings.account_equity_usdt * settings.max_position_pct / 100 * leverage
    quantity = min(risk_budget / loss_per_unit, position_cap / max(worst_entry, 1e-12))
    entry_low, entry_high = sorted((first, third))
    return (
        TradePlan(
            entry_low=float(entry_low),
            entry_high=float(entry_high),
            stop_loss=float(stop),
            take_profit_1=float(tp1),
            take_profit_2=float(tp2),
            take_profit_3=float(tp3),
            risk_reward_2=float(rr),
            invalidation=(
                "Отменить сценарий при достижении стопа до заполнения либо при сломе "
                "15m структуры. После отмены не усреднять и не переносить стоп."
            ),
            expires_at=now + timedelta(minutes=settings.rid_entry_expiry_minutes),
            suggested_notional=float(quantity * worst_entry),
            suggested_quantity=float(quantity),
            risk_amount=float(quantity * loss_per_unit),
            scale_entries=(float(first), float(second), float(third)),
            scale_allocations_pct=(50, 30, 20),
            recommended_leverage=leverage,
            max_leverage=min(3, settings.max_leverage),
            holding_horizon=f"до {settings.rid_max_holding_hours} часов; раньше при стопе",
        ),
        blockers,
    )


def analyze_rid_pattern(
    symbol: str,
    exchange: str,
    ticker: Ticker,
    series: dict[str, list[Candle]],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> Signal:
    now = now or datetime.now(UTC)
    bars5 = series["5"]
    for interval in ("5", "15", "60"):
        _validate_closed_series(series[interval], interval, now)
    features = {key: compute_features(value) for key, value in series.items()}
    f5, f15, f60 = features["5"], features["15"], features["60"]
    evaluations: list[tuple[int, Side, ImpulseLeg, dict[str, float], list[str], list[str]]] = []

    sides = (Side.LONG, Side.SHORT) if settings.rid_short_enabled else (Side.LONG,)
    for side in sides:
        leg = _latest_impulse(
            bars5,
            f5.atr14,
            side,
            min_atr=settings.rid_min_impulse_atr,
            max_atr=settings.rid_max_impulse_atr,
            min_rvol=settings.rid_min_impulse_rvol,
        )
        if leg is None:
            continue
        post = bars5[leg.extreme_index + 1 :]
        if len(post) < 2:
            continue
        impulse_volume = max(bar.volume for bar in bars5[leg.start_index : leg.extreme_index + 1])
        pullback_volume_ratio = _median([bar.volume for bar in post[:-1]]) / max(
            impulse_volume, 1e-12
        )
        if pullback_volume_ratio > settings.rid_max_pullback_volume_ratio:
            continue

        size = abs(leg.extreme - leg.origin)
        latest = bars5[-1]
        previous = bars5[-4:-1]
        if side is Side.LONG:
            pullback_extreme = min(bar.low for bar in post)
            retracement = (leg.extreme - pullback_extreme) / size
            recovery = (latest.close - pullback_extreme) / size
            reactivated = (
                latest.close > latest.open
                and latest.close > max(bar.close for bar in previous)
                and latest.close <= leg.extreme + 0.30 * f5.atr14
            )
            trend15 = f15.ema20 > f15.ema50 and f15.dmi_spread > -8
            trend60 = f60.ema20 > f60.ema50 or f60.supertrend_direction > 0
            flow_support = sum(
                (
                    ticker.taker_buy_ratio is not None and ticker.taker_buy_ratio >= 0.52,
                    ticker.orderbook_imbalance is not None and ticker.orderbook_imbalance >= 0.03,
                    ticker.open_interest_change_pct is not None
                    and ticker.open_interest_change_pct > 0,
                )
            )
        else:
            pullback_extreme = max(bar.high for bar in post)
            retracement = (pullback_extreme - leg.extreme) / size
            recovery = (pullback_extreme - latest.close) / size
            reactivated = (
                latest.close < latest.open
                and latest.close < min(bar.close for bar in previous)
                and latest.close >= leg.extreme - 0.30 * f5.atr14
            )
            trend15 = f15.ema20 < f15.ema50 and f15.dmi_spread < 8
            trend60 = f60.ema20 < f60.ema50 or f60.supertrend_direction < 0
            flow_support = sum(
                (
                    ticker.taker_buy_ratio is not None and ticker.taker_buy_ratio <= 0.48,
                    ticker.orderbook_imbalance is not None and ticker.orderbook_imbalance <= -0.03,
                    ticker.open_interest_change_pct is not None
                    and ticker.open_interest_change_pct > 0,
                )
            )
        if not settings.rid_min_retracement <= retracement <= settings.rid_max_retracement:
            continue
        if recovery < 0.18 or not reactivated or not trend15:
            continue

        retrace_points = 15 if 0.30 <= retracement <= 0.70 else 8
        score = min(
            100,
            round(
                20
                + min(15, (leg.size_atr - settings.rid_min_impulse_atr) * 5 + 7)
                + min(15, (leg.volume_ratio - settings.rid_min_impulse_rvol) * 4 + 7)
                + min(15, max(0, 1 - pullback_volume_ratio) * 18)
                + retrace_points
                + 18
                + (10 if trend60 else 3)
                + min(7, flow_support * 2.5)
            ),
        )
        pattern = "контролируемый откат" if retracement <= 0.75 else "сбор ликвидности и возврат"
        reasons = [
            f"Первичный 5m импульс {leg.size_atr:.1f} ATR с объёмом {leg.volume_ratio:.1f}× нормы",
            f"После импульса объём сжался до {pullback_volume_ratio:.2f} от пикового",
            f"Откат {retracement:.0%}: {pattern}; восстановление {recovery:.0%} импульса",
            "Последняя закрытая 5m свеча вернула локальные закрытия — "
            "повторная активация подтверждена",
            f"15m тренд подтверждает {side.value}; 1h "
            f"{'подтверждает' if trend60 else 'пока нейтрален'}",
        ]
        risks = []
        if retracement > 0.75:
            risks.append("Глубокий вынос ликвидности: вероятность повторного теста повышена")
        if not trend60:
            risks.append("Часовой тренд ещё не полностью согласован с направлением")
        if flow_support == 0:
            risks.append("Деривативный поток не дал дополнительного подтверждения")
        metrics = {
            "rid_impulse_atr": leg.size_atr,
            "rid_impulse_rvol": leg.volume_ratio,
            "rid_retracement": retracement,
            "rid_recovery": recovery,
            "rid_pullback_volume_ratio": pullback_volume_ratio,
            "rid_flow_confirmations": float(flow_support),
            "rid_trend60_confirmed": float(trend60),
        }
        evaluations.append((score, side, leg, metrics, reasons, risks))

    if not evaluations:
        return Signal(
            symbol=symbol,
            exchange=exchange,
            side=Side.NO_TRADE,
            confidence=0,
            score=0,
            regime="RID_NONE",
            price=ticker.last,
            created_at=now,
            blockers=["Нет закрытой последовательности импульс → тихий откат → реактивация"],
            features=features,
            required_confidence=settings.rid_manual_min_score,
            strategy_version=CURRENT_RID_STRATEGY_VERSION,
        )

    score, side, leg, metrics, reasons, risks = max(evaluations, key=lambda row: row[0])
    plan, blockers = _plan(side, ticker, bars5, leg, f5.atr14, settings, now)
    if score < settings.rid_manual_min_score:
        blockers.append(f"RID score {score}/100 ниже ручного порога")
        plan = None
    return Signal(
        symbol=symbol,
        exchange=exchange,
        side=side,
        confidence=score,
        score=float(score),
        regime="RID_CONTINUATION",
        price=ticker.last,
        created_at=now,
        reasons=reasons,
        risks=risks,
        blockers=blockers,
        features=features,
        market_context=metrics,
        plan=plan,
        required_confidence=settings.rid_manual_min_score,
        strategy_version=CURRENT_RID_STRATEGY_VERSION,
    )


def is_rid_auto_candidate(signal: Signal, settings: Settings) -> bool:
    """Fail closed until both live evidence and forward paper evidence exist."""
    metrics = signal.market_context
    return bool(
        signal.actionable
        and signal.confidence >= settings.rid_auto_min_score
        and signal.calibration_samples >= settings.rid_auto_min_samples
        and signal.estimated_success_pct is not None
        and signal.estimated_success_pct >= settings.rid_auto_min_win_rate
        and signal.recent_expectancy_r is not None
        and signal.recent_expectancy_r >= settings.rid_auto_min_expectancy_r
        and 0.30 <= metrics.get("rid_retracement", 0.0) <= 0.50
        and metrics.get("rid_trend60_confirmed", 0.0) >= 1
        and metrics.get("rid_flow_confirmations", 0.0) >= 1
    )


class RidScanner:
    def __init__(self, exchange: ExchangeClient, store: SignalStore, settings: Settings) -> None:
        self.exchange = exchange
        self.store = store
        self.settings = settings
        self.scan_lock = asyncio.Lock()
        self.last_report: ScanReport | None = None
        self.last_error: str | None = None

    def _universe(self, tickers: list[Ticker]) -> list[Ticker]:
        rows = [
            item
            for item in tickers
            if item.symbol not in self.settings.excluded_symbol_set
            and item.turnover_24h >= self.settings.min_volume_usdt
            and item.last > 0
            and item.bid > 0
            and item.ask >= item.bid
            and item.spread_bps <= self.settings.max_spread_bps
        ]
        rows.sort(key=lambda item: item.turnover_24h, reverse=True)
        return rows[: self.settings.rid_universe_size]

    @staticmethod
    def _quick_rank(bars: list[Candle]) -> float:
        if len(bars) < 40:
            return 0.0
        recent = bars[-30:]
        baseline = _median([bar.volume for bar in bars[-50:-30]])
        if baseline <= 0:
            return 0.0
        move = (max(bar.high for bar in recent) - min(bar.low for bar in recent)) / max(
            recent[-1].close, 1e-12
        )
        burst = max(bar.volume for bar in recent) / baseline
        return move * 100 + min(burst, 10)

    async def scan(self) -> ScanReport:
        async with self.scan_lock:
            started = datetime.now(UTC)
            errors: list[str] = []
            tickers = await self.exchange.tickers()
            universe = self._universe(tickers)
            quick = await asyncio.gather(
                *(self.exchange.candles(item.symbol, "5", 260) for item in universe),
                return_exceptions=True,
            )
            ranked: list[tuple[float, Ticker, list[Candle]]] = []
            for ticker, result in zip(universe, quick, strict=True):
                if isinstance(result, BaseException):
                    errors.append(f"{ticker.symbol}: {type(result).__name__}")
                else:
                    ranked.append((self._quick_rank(result), ticker, result))
            ranked.sort(key=lambda row: row[0], reverse=True)
            # Rank controls processing order, not eligibility. A large move must
            # never exclude a quieter valid pullback further down the list.
            shortlisted = ranked

            async def analyze(row: tuple[float, Ticker, list[Candle]]) -> Signal:
                _, ticker, bars5 = row
                bars15, bars60 = await asyncio.gather(
                    self.exchange.candles(ticker.symbol, "15", 260),
                    self.exchange.candles(ticker.symbol, "60", 260),
                )
                candles = {"5": bars5, "15": bars15, "60": bars60}
                signal = await asyncio.to_thread(
                    analyze_rid_pattern,
                    ticker.symbol,
                    self.exchange.name,
                    ticker,
                    candles,
                    self.settings,
                )
                # Expensive OI/taker/book requests are made only after the candle core
                # has found a valid executable pattern.
                if (
                    signal.side is not Side.NO_TRADE
                    and self.settings.market_microstructure_enabled
                ):
                    enriched = await self.exchange.enrich_ticker(ticker)
                    signal = await asyncio.to_thread(
                        analyze_rid_pattern,
                        ticker.symbol,
                        self.exchange.name,
                        enriched,
                        candles,
                        self.settings,
                    )
                if signal.actionable:
                    await self._attach_calibration(signal)
                    await self.store.save(signal)
                    if self.settings.paper_tracking_enabled:
                        await self.store.record_paper_candidate(
                            signal,
                            max_holding_hours=self.settings.rid_max_holding_hours,
                            dedup_minutes=self.settings.rid_paper_dedup_minutes,
                        )
                return signal

            analyzed = []
            for offset in range(0, len(shortlisted), self.settings.rid_shortlist_size):
                batch = shortlisted[offset : offset + self.settings.rid_shortlist_size]
                analyzed.extend(await asyncio.gather(
                    *(analyze(row) for row in batch), return_exceptions=True
                ))
            signals: list[Signal] = []
            rejected: Counter[str] = Counter()
            completed = 0
            for row, result in zip(shortlisted, analyzed, strict=True):
                if isinstance(result, BaseException):
                    errors.append(f"{row[1].symbol}: {type(result).__name__}")
                    log.warning("RID analysis failed for %s: %s", row[1].symbol, result)
                elif result.actionable:
                    completed += 1
                    signals.append(result)
                else:
                    completed += 1
                    rejected.update(result.blockers or ["Условия входа не выполнены"])
            signals.sort(key=lambda item: item.confidence, reverse=True)
            report = ScanReport(
                exchange=self.exchange.name,
                started_at=started,
                finished_at=datetime.now(UTC),
                universe_count=len(universe),
                analyzed_count=completed,
                signals=tuple(signals),
                errors=tuple(errors),
                diagnostics=tuple(
                    f"{reason}: {count}" for reason, count in rejected.most_common(5)
                ),
            )
            self.last_report = report
            self.last_error = None
            await self.store.set_runtime("last_rid_scan", report.finished_at.isoformat())
            return report

    async def _attach_calibration(self, signal: Signal) -> None:
        stats = await self.store.calibration(
            side=signal.side,
            strategy_version=CURRENT_RID_STRATEGY_VERSION,
            limit=self.settings.calibration_lookback,
        )
        signal.calibration_samples = stats.sample_size
        signal.recent_expectancy_r = stats.expectancy_r if stats.sample_size else None
        if stats.sample_size >= self.settings.rid_auto_min_samples:
            signal.estimated_success_pct = stats.win_rate
            signal.success_interval_low = stats.interval_low
            signal.success_interval_high = stats.interval_high

    async def monitor(self, callback, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                report = await asyncio.wait_for(self.scan(), timeout=75)
                for signal in report.signals[:1]:
                    if not is_rid_auto_candidate(signal, self.settings):
                        continue
                    symbol_ok, budget_ok = await asyncio.gather(
                        self.store.strict_alert_allowed(
                            signal.fingerprint, self.settings.rid_symbol_cooldown_minutes
                        ),
                        self.store.notification_budget_available(
                            "rid",
                            cooldown_minutes=self.settings.rid_global_cooldown_minutes,
                            max_per_day=self.settings.rid_max_alerts_per_day,
                        ),
                    )
                    if not symbol_ok or not budget_ok:
                        continue
                    await callback(signal)
                    await self.store.mark_alerted(
                        signal,
                        track_paper=False,
                        max_holding_hours=self.settings.rid_max_holding_hours,
                    )
                    await self.store.mark_notification_budget("rid")
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("RID automatic scan failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.rid_scan_interval_seconds
                )
