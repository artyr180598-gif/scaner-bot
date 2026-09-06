from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptopilot.config import Settings
from cryptopilot.cross_exchange import CrossExchangeSnapshot, verify_cross_exchange
from cryptopilot.exchange import ExchangeClient
from cryptopilot.flow import FlowSnapshot, FlowTracker
from cryptopilot.indicators import compute_features
from cryptopilot.liquidity import LiquiditySnapshot, LiquidityTracker
from cryptopilot.models import FeatureSet, Side, Ticker, TradePlan
from cryptopilot.prime_plan import build_prime_plan

log = logging.getLogger(__name__)
SCAN_TIMEOUT_SECONDS = 42
TICKER_STAGE_TIMEOUT_SECONDS = 7
CONFIRMATION_STAGE_TIMEOUT_SECONDS = 6
QUICK_STAGE_TIMEOUT_SECONDS = 12
DEEP_STAGE_TIMEOUT_SECONDS = 16


def _consume_task_result(task: asyncio.Task) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()


def _cancel_detached(tasks) -> None:
    for task in tasks:
        task.cancel()
        task.add_done_callback(_consume_task_result)


async def _hard_wait(awaitable, timeout_seconds: float):
    """Return on deadline even if underlying network cancellation is slow."""
    task = asyncio.create_task(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        _cancel_detached((task,))
        await asyncio.sleep(0)
        raise
    if task not in done:
        _cancel_detached((task,))
        await asyncio.sleep(0)
        raise TimeoutError(f"stage exceeded {timeout_seconds:.0f}s")
    return task.result()


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
    structure_4h: str
    recent_move_15m_pct: float
    rvol: float
    oi_change_pct: float | None
    taker_buy_ratio: float | None
    orderbook_imbalance: float | None
    funding_pct: float
    spot_taker_buy_ratio: float | None
    spot_orderbook_imbalance: float | None
    spot_block_trade_buy_ratio: float | None
    spot_block_trade_notional: float | None
    spot_perp_basis_bps: float | None
    live_delta_ratio_60s: float | None
    live_cvd_ratio_5m: float | None
    live_volume_burst_ratio: float | None
    live_oi_change_2m_pct: float | None
    live_oi_acceleration_pct_per_min: float | None
    live_absorption: str | None
    persistent_book_imbalance: float | None
    bid_wall_ratio: float | None
    ask_wall_ratio: float | None
    bid_wall_persistence_seconds: float
    ask_wall_persistence_seconds: float
    bid_replenishment_usdt_60s: float
    ask_replenishment_usdt_60s: float
    long_liquidation_usdt_60s: float
    short_liquidation_usdt_60s: float
    cross_exchange: str | None
    cross_confirmations: int
    cross_conflicts: int
    cross_price_divergence_bps: float | None
    cross_summary: tuple[str, ...]
    plan: TradePlan | None
    distance_to_trigger_pct: float
    prime_score: int
    prime_ready: bool
    created_at: datetime
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    prime_reasons: tuple[str, ...] = ()
    prime_blockers: tuple[str, ...] = ()


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

    def __init__(
        self,
        exchange: ExchangeClient,
        settings: Settings,
        flow_tracker: FlowTracker | None = None,
        liquidity_tracker: LiquidityTracker | None = None,
        confirmation_exchange: ExchangeClient | None = None,
    ) -> None:
        self.exchange = exchange
        self.settings = settings
        self.flow_tracker = flow_tracker
        self.liquidity_tracker = liquidity_tracker
        self.confirmation_exchange = confirmation_exchange
        self._lock = asyncio.Lock()
        self.last_report: SmartMoneyReport | None = None
        self._flow_watchlist: dict[str, tuple[Side, float]] = {}
        self._prime_candidates: tuple[SmartMoneySetup, ...] = ()
        self._shadow_candidates: tuple[SmartMoneySetup, ...] = ()

    async def scan(self) -> SmartMoneyReport:
        # Hard deadline for callers. asyncio.wait returns on time even when
        # cancellation cleanup in a slow socket is delayed.
        task = asyncio.create_task(self._scan())
        done, _ = await asyncio.wait({task}, timeout=SCAN_TIMEOUT_SECONDS)
        if task not in done:
            _cancel_detached((task,))
            await asyncio.sleep(0)
            raise TimeoutError(f"PRIME scan exceeded {SCAN_TIMEOUT_SECONDS}s")
        return task.result()

    async def _scan(self) -> SmartMoneyReport:
        async with self._lock:
            if self.last_report is not None and (
                0 <= (datetime.now(UTC) - self.last_report.finished_at).total_seconds() < 15
            ):
                return self.last_report
            self._prime_candidates = ()
            self._shadow_candidates = ()
            started = datetime.now(UTC)
            errors: list[str] = []
            tickers = await _hard_wait(
                self.exchange.tickers(),
                TICKER_STAGE_TIMEOUT_SECONDS,
            )
            universe = self._universe(tickers)
            confirmation_map: dict[str, Ticker] = {}
            if (
                self.settings.prime_cross_exchange_enabled
                and self.confirmation_exchange is not None
            ):
                try:
                    confirmation_tickers = await _hard_wait(
                        self.confirmation_exchange.tickers(),
                        CONFIRMATION_STAGE_TIMEOUT_SECONDS,
                    )
                    confirmation_map = {item.symbol: item for item in confirmation_tickers}
                except Exception as exc:
                    errors.append(f"{self.confirmation_exchange.name}: {type(exc).__name__}")
                    log.warning(
                        "Cross-exchange ticker snapshot unavailable: %s",
                        type(exc).__name__,
                    )

            quick_tasks = {
                asyncio.create_task(self._quick(item)): item for item in universe
            }
            try:
                quick_done, quick_pending = await asyncio.wait(
                    set(quick_tasks),
                    timeout=QUICK_STAGE_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                _cancel_detached(set(quick_tasks))
                await asyncio.sleep(0)
                raise
            if quick_pending:
                _cancel_detached(quick_pending)
                errors.append(f"quick-stage timeout: {len(quick_pending)} markets skipped")
            ranked: list[tuple[float, Ticker, FeatureSet]] = []
            for task in quick_done:
                ticker = quick_tasks[task]
                try:
                    feature = task.result()
                except BaseException as exc:
                    errors.append(f"{ticker.symbol}: {type(exc).__name__}")
                    continue
                ranked.append((self._pre_score(feature), ticker, feature))

            ranked.sort(key=lambda item: item[0], reverse=True)
            prime_ranked = sorted(
                (
                    (self._prime_pre_score(feature), ticker, feature)
                    for _, ticker, feature in ranked
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            self._flow_watchlist = self._build_flow_watchlist(prime_ranked[:24])

            # Cheap 15m discovery still sees the full liquid universe. Expensive
            # spot/OI/order-book/cross-exchange enrichment is reserved for the
            # strongest eight; live flow watches a wider list and can recheck a
            # symbol immediately when pressure starts.
            deep_limit = min(max(self.settings.shortlist_size // 2, 6), 8)
            candidates = self._deep_candidates(
                prime_ranked,
                ranked if self.settings.smart_money_include_post_breakout else [],
                deep_limit,
            )

            deep_tasks = {
                asyncio.create_task(
                    self._deep(
                        ticker,
                        feature15,
                        confirmation_map.get(ticker.symbol),
                    )
                ): ticker
                for _, ticker, feature15 in candidates
            }
            try:
                deep_done, deep_pending = await asyncio.wait(
                    set(deep_tasks),
                    timeout=DEEP_STAGE_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                _cancel_detached(set(deep_tasks))
                await asyncio.sleep(0)
                raise
            if deep_pending:
                _cancel_detached(deep_pending)
                errors.append(f"deep-stage timeout: {len(deep_pending)} markets skipped")
            setups: list[SmartMoneySetup] = []
            for task in deep_done:
                ticker = deep_tasks[task]
                try:
                    result = task.result()
                except BaseException as exc:
                    errors.append(f"{ticker.symbol}: {type(exc).__name__}")
                    continue
                if result is not None:
                    setups.append(result)

            # User-facing ranking is deliberately pre-move first. ENTRY is confirmation
            # after the structural break and must never outrank ARMED/WATCH candidates.
            stage_rank = {"ARMED": 3, "WATCH": 2, "ENTRY": 0}
            setups.sort(
                key=lambda item: (
                    item.prime_score if item.stage != "ENTRY" else -1,
                    stage_rank.get(item.stage, 0),
                    item.score,
                ),
                reverse=True,
            )
            self._prime_candidates = tuple(
                sorted(
                    (item for item in setups if item.prime_ready),
                    key=lambda item: (item.prime_score, item.score),
                    reverse=True,
                )[:3]
            )
            self._shadow_candidates = tuple(
                sorted(
                    (
                        item
                        for item in setups
                        if item.plan is not None
                        and item.stage != "ENTRY"
                        and item.prime_score >= self.settings.prime_shadow_min_score
                    ),
                    key=lambda item: (item.prime_score, item.score),
                    reverse=True,
                )[:6]
            )
            visible_setups = (
                setups
                if self.settings.smart_money_include_post_breakout
                else [item for item in setups if item.stage != "ENTRY"]
            )
            report = SmartMoneyReport(
                exchange=self.exchange.name,
                started_at=started,
                finished_at=datetime.now(UTC),
                universe_count=len(universe),
                analyzed_count=len(candidates),
                setups=tuple(visible_setups[:8]),
                errors=tuple(errors[:12]),
            )
            self.last_report = report
            return report

    def flow_watchlist(self) -> dict[str, tuple[Side, float]]:
        """Preselected candidates are streamed even before the deep score reaches WATCH."""
        return dict(self._flow_watchlist)

    @staticmethod
    def _deep_candidates(prime_ranked, active_ranked, limit):
        """Use the same minimum preparation score as the streaming watchlist.

        Do not pad an empty early shortlist with active movers by default.
        """
        selected = []
        seen = set()
        early = [
            row
            for row in prime_ranked
            if row[0] >= 28 and not row[2].breakout_up and not row[2].breakout_down
        ]
        for row in early[:limit] + active_ranked[:8]:
            if row[1].symbol in seen:
                continue
            if len(selected) >= limit:
                break
            selected.append(row)
            seen.add(row[1].symbol)
        return selected

    def prime_candidates(self) -> tuple[SmartMoneySetup, ...]:
        """Highest-quality pre-move candidates; intentionally tiny to avoid alert spam."""
        return self._prime_candidates

    def prepare_candidates(self) -> tuple[SmartMoneySetup, ...]:
        """Strong early observations that are not yet strict PRIME entries."""
        report = self.last_report
        if report is None:
            return ()
        values = [
            item
            for item in report.setups
            if is_prepare_candidate(item, self.settings)
        ]
        values.sort(key=lambda item: (item.prime_score, item.score), reverse=True)
        return tuple(values[:3])

    def shadow_candidates(self) -> tuple[SmartMoneySetup, ...]:
        """Broader silent sample used only to learn which PRIME patterns work later."""
        return self._shadow_candidates

    async def analyze_symbol(self, symbol: str) -> SmartMoneySetup | None:
        """Run the full pre-move stack for one requested symbol."""
        normalized = symbol.upper().replace("/", "").replace("-", "")
        if not normalized.endswith("USDT"):
            normalized += "USDT"
        tickers = await self.exchange.tickers()
        ticker = next((item for item in tickers if item.symbol == normalized), None)
        if ticker is None:
            raise ValueError(
                f"{normalized} is not an active USDT perpetual on {self.exchange.name}"
            )
        confirmation_ticker: Ticker | None = None
        if self.settings.prime_cross_exchange_enabled and self.confirmation_exchange is not None:
            try:
                secondary = await self.confirmation_exchange.tickers()
                confirmation_ticker = next(
                    (item for item in secondary if item.symbol == normalized),
                    None,
                )
            except Exception as exc:
                log.debug(
                    "Secondary exchange unavailable for %s manual analysis: %s",
                    normalized,
                    type(exc).__name__,
                )
        feature15 = await self._quick(ticker)
        return await self._deep(ticker, feature15, confirmation_ticker)

    @staticmethod
    def _build_flow_watchlist(
        ranked: list[tuple[float, Ticker, FeatureSet]],
    ) -> dict[str, tuple[Side, float]]:
        result: dict[str, tuple[Side, float]] = {}
        for pre_score, ticker, feature in ranked:
            if pre_score < 28:
                continue
            long_score = _structure_score(feature, Side.LONG)
            short_score = _structure_score(feature, Side.SHORT)
            if long_score == short_score:
                if feature.range_position20 >= 0.62:
                    bias = Side.LONG
                elif feature.range_position20 <= 0.38:
                    bias = Side.SHORT
                else:
                    continue
            else:
                bias = Side.LONG if long_score > short_score else Side.SHORT
            if feature.breakout_up or feature.breakout_down:
                continue
            directional_position = (
                feature.range_position20 if bias is Side.LONG else 1 - feature.range_position20
            )
            if not 0.62 <= directional_position <= 0.96:
                continue
            trigger = feature.range_high20 if bias is Side.LONG else feature.range_low20
            if trigger > 0:
                result[ticker.symbol] = (bias, trigger)
        return result

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

    @staticmethod
    def _prime_pre_score(feature: FeatureSet) -> float:
        """Cheap first pass that prefers compression before volume expansion."""
        if feature.breakout_up or feature.breakout_down:
            return 0.0
        aligned = (
            feature.close > feature.ema20 > feature.ema50
            or feature.close < feature.ema20 < feature.ema50
        )
        directional_position = max(
            feature.range_position20,
            1 - feature.range_position20,
        )
        score = 12.0 if aligned else 0.0
        if 0.65 <= directional_position <= 0.96:
            score += 10
        if feature.keltner_squeeze_ratio <= 1.05:
            score += 12
        elif feature.keltner_squeeze_ratio <= 1.15:
            score += 5
        if feature.bb_width_regime_ratio <= 0.95:
            score += 6
        if 0.65 <= feature.relative_volume20 <= 1.25:
            score += 8
        elif feature.relative_volume20 > 1.5:
            score -= 12
        if abs(feature.vwap_distance_atr) <= 1.5:
            score += 4
        return max(0.0, score)

    async def _deep(
        self,
        ticker: Ticker,
        feature15: FeatureSet,
        confirmation_ticker: Ticker | None = None,
    ) -> SmartMoneySetup | None:
        enriched, candles_1h, candles_4h, candles_5m = await asyncio.gather(
            self.exchange.enrich_ticker(ticker),
            self.exchange.candles(ticker.symbol, "60", 240),
            self.exchange.candles(ticker.symbol, "240", 240),
            self.exchange.candles(ticker.symbol, "5", 80),
        )
        feature1h = compute_features(candles_1h)
        feature4h = compute_features(candles_4h)
        recent_move_15m_pct = _recent_close_move_pct(candles_5m, 3)
        flow = self.flow_tracker.snapshot(ticker.symbol) if self.flow_tracker is not None else None
        liquidity = (
            self.liquidity_tracker.snapshot(
                ticker.symbol,
                int(datetime.now(UTC).timestamp() * 1000),
            )
            if self.liquidity_tracker is not None
            else None
        )

        long_score, long_reasons, long_warnings = _direction_score(
            Side.LONG, feature15, feature1h, enriched, flow
        )
        short_score, short_reasons, short_warnings = _direction_score(
            Side.SHORT, feature15, feature1h, enriched, flow
        )
        if max(long_score, short_score) < 62:
            return None

        bias = Side.LONG if long_score >= short_score else Side.SHORT
        score = int(round(max(long_score, short_score)))
        reasons = long_reasons if bias is Side.LONG else short_reasons
        warnings = long_warnings if bias is Side.LONG else short_warnings
        stage = _stage(bias, score, feature15, feature1h, enriched, flow)

        if stage == "WATCH" and score < 65:
            return None

        cross: CrossExchangeSnapshot | None = None
        if (
            self.settings.prime_cross_exchange_enabled
            and self.confirmation_exchange is not None
            and confirmation_ticker is not None
        ):
            try:
                cross = await verify_cross_exchange(
                    self.confirmation_exchange,
                    confirmation_ticker,
                    bias,
                    feature15.close,
                )
            except Exception as exc:
                log.debug(
                    "Cross-exchange PRIME check unavailable for %s: %s",
                    ticker.symbol,
                    type(exc).__name__,
                )

        trigger = feature15.range_high20 if bias is Side.LONG else feature15.range_low20
        invalidation = feature15.range_low20 if bias is Side.LONG else feature15.range_high20
        distance_to_trigger_pct = (
            abs(feature15.close / trigger - 1) * 100 if trigger > 0 else float("inf")
        )
        prime_score, prime_reasons, prime_blockers = _pre_move_score(
            bias,
            feature15,
            feature1h,
            feature4h,
            enriched,
            flow,
            liquidity,
            recent_move_15m_pct,
            self.settings,
        )

        if (
            self.settings.prime_cross_exchange_enabled
            and self.settings.prime_cross_exchange_required
            and self.confirmation_exchange is not None
            and cross is None
        ):
            prime_blockers.append(
                f"{self.confirmation_exchange.name}: обязательное второе подтверждение "
                "временно недоступно"
            )

        if cross is not None:
            if (
                cross.price_divergence_bps is not None
                and cross.price_divergence_bps
                > self.settings.prime_cross_exchange_max_price_divergence_bps
            ):
                prime_blockers.append(
                    f"{cross.exchange}: цены расходятся на {cross.price_divergence_bps:.1f} bps"
                )
            if cross.conflicts >= 2:
                prime_blockers.append(
                    f"{cross.exchange}: {cross.conflicts} независимых признака против сценария"
                )
            elif cross.confirmations < self.settings.prime_cross_exchange_min_confirmations:
                prime_blockers.append(
                    f"{cross.exchange}: подтверждений только "
                    f"{cross.confirmations}/"
                    f"{self.settings.prime_cross_exchange_min_confirmations}"
                )
            else:
                prime_score = min(100, prime_score + min(8, cross.confirmations * 2))
                prime_reasons.append(
                    f"{cross.exchange}: {cross.confirmations} независимых "
                    "подтверждения того же сценария"
                )
                prime_reasons.extend(cross.reasons[:2])

        plan_result = build_prime_plan(
            bias,
            feature15.close,
            trigger,
            candles_5m,
            feature15,
            self.settings,
        )
        prime_blockers.extend(plan_result.blockers)
        prime_ready = (
            prime_score >= self.settings.prime_min_score
            and not prime_blockers
            and stage != "ENTRY"
            and plan_result.plan is not None
        )
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
            structure_4h=_structure_label(feature4h),
            recent_move_15m_pct=recent_move_15m_pct,
            rvol=feature15.relative_volume20,
            oi_change_pct=enriched.open_interest_change_pct,
            taker_buy_ratio=enriched.taker_buy_ratio,
            orderbook_imbalance=enriched.orderbook_imbalance,
            funding_pct=enriched.funding_rate * 100,
            spot_taker_buy_ratio=enriched.spot_taker_buy_ratio,
            spot_orderbook_imbalance=enriched.spot_orderbook_imbalance,
            spot_block_trade_buy_ratio=enriched.spot_block_trade_buy_ratio,
            spot_block_trade_notional=enriched.spot_block_trade_notional,
            spot_perp_basis_bps=enriched.spot_perp_basis_bps,
            live_delta_ratio_60s=flow.delta_ratio_60s if flow and flow.fresh else None,
            live_cvd_ratio_5m=flow.cvd_ratio_5m if flow and flow.fresh else None,
            live_volume_burst_ratio=flow.volume_burst_ratio if flow and flow.fresh else None,
            live_oi_change_2m_pct=flow.oi_change_2m_pct if flow and flow.fresh else None,
            live_oi_acceleration_pct_per_min=(
                flow.oi_acceleration_pct_per_min if flow and flow.fresh else None
            ),
            live_absorption=flow.absorption if flow and flow.fresh else None,
            persistent_book_imbalance=liquidity.imbalance_top10 if liquidity else None,
            bid_wall_ratio=liquidity.bid_wall_ratio if liquidity else None,
            ask_wall_ratio=liquidity.ask_wall_ratio if liquidity else None,
            bid_wall_persistence_seconds=(
                liquidity.bid_wall_persistence_seconds if liquidity else 0.0
            ),
            ask_wall_persistence_seconds=(
                liquidity.ask_wall_persistence_seconds if liquidity else 0.0
            ),
            bid_replenishment_usdt_60s=(liquidity.bid_replenishment_usdt_60s if liquidity else 0.0),
            ask_replenishment_usdt_60s=(liquidity.ask_replenishment_usdt_60s if liquidity else 0.0),
            long_liquidation_usdt_60s=(liquidity.long_liquidation_usdt_60s if liquidity else 0.0),
            short_liquidation_usdt_60s=(liquidity.short_liquidation_usdt_60s if liquidity else 0.0),
            cross_exchange=cross.exchange if cross else None,
            cross_confirmations=cross.confirmations if cross else 0,
            cross_conflicts=cross.conflicts if cross else 0,
            cross_price_divergence_bps=(cross.price_divergence_bps if cross else None),
            cross_summary=(tuple(cross.reasons[:3] + cross.warnings[:2]) if cross else ()),
            plan=plan_result.plan,
            distance_to_trigger_pct=distance_to_trigger_pct,
            prime_score=prime_score,
            prime_ready=prime_ready,
            created_at=datetime.now(UTC),
            reasons=tuple(reasons[:5]),
            warnings=tuple(warnings[:4]),
            prime_reasons=tuple(prime_reasons[:6]),
            prime_blockers=tuple(prime_blockers[:4]),
        )


def is_prepare_candidate(item: SmartMoneySetup, settings: Settings) -> bool:
    """High-quality pre-move observation; deliberately not labelled as an entry."""
    if item.prime_ready or item.stage != "ARMED" or item.bias is Side.NO_TRADE:
        return False
    if item.prime_score < settings.prepare_min_score:
        return False
    if not (
        settings.prime_min_trigger_distance_pct
        <= item.distance_to_trigger_pct
        <= settings.prime_max_trigger_distance_pct
    ):
        return False
    directional_move = (
        item.recent_move_15m_pct
        if item.bias is Side.LONG
        else -item.recent_move_15m_pct
    )
    if directional_move > settings.prime_max_directional_move_15m_pct:
        return False
    if item.rvol > 1.35:
        return False
    if item.cross_conflicts >= 2:
        return False
    if item.oi_change_pct is not None and item.oi_change_pct < -1:
        return False
    directional_funding = item.funding_pct if item.bias is Side.LONG else -item.funding_pct
    if directional_funding > settings.flow_max_directional_funding_pct:
        return False
    if item.spot_taker_buy_ratio is not None:
        directional_spot = (
            item.spot_taker_buy_ratio
            if item.bias is Side.LONG
            else 1 - item.spot_taker_buy_ratio
        )
        if directional_spot < 0.45:
            return False
    return True


async def refresh_smart_money_watchlist(
    scanner: SmartMoneyScanner,
    stop: asyncio.Event,
    interval_seconds: int = 300,
    initial_delay_seconds: int = 45,
    on_report: Callable[[SmartMoneyReport], Awaitable[None]] | None = None,
) -> None:
    """Continuously refresh candidates so streaming flow can watch them before BOS."""
    if initial_delay_seconds > 0:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=initial_delay_seconds)
    while not stop.is_set():
        report = scanner.last_report
        age = (
            (datetime.now(UTC) - report.finished_at).total_seconds()
            if report is not None
            else float("inf")
        )
        if age >= interval_seconds and not scanner._lock.locked():
            try:
                refreshed = await scanner.scan()
                if on_report is not None:
                    await on_report(refreshed)
            except Exception as exc:
                log.warning("Smart money auto refresh failed: %s", type(exc).__name__)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=30)


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
    if feature.breakout_up if bullish else feature.breakout_down:
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
    flow: FlowSnapshot | None = None,
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

    if flow is not None and flow.fresh:
        live_score, live_reasons, live_warnings = _live_flow_adjustment(side, flow)
        score += live_score
        reasons.extend(live_reasons)
        warnings.extend(live_warnings)

    return max(0.0, min(score, 100.0)), reasons, warnings


def _pre_move_score(
    side: Side,
    f15: FeatureSet,
    f1h: FeatureSet,
    f4h: FeatureSet,
    ticker: Ticker,
    flow: FlowSnapshot | None,
    liquidity: LiquiditySnapshot | None,
    recent_move_15m_pct: float,
    settings: Settings,
) -> tuple[int, list[str], list[str]]:
    """Score conditions that exist before the obvious order-flow expansion.

    The goal is not to predict hidden orders. It ranks a small set of coiled, liquid,
    higher-timeframe-aligned markets while explicitly rejecting anything already moving.
    """
    bullish = side is Side.LONG
    score = 0.0
    reasons: list[str] = []
    blockers: list[str] = []

    breakout = f15.breakout_up if bullish else f15.breakout_down
    if breakout:
        blockers.append("BOS уже произошёл — для раннего входа поздно")

    htf_aligned = (
        f1h.close > f1h.ema50 > f1h.ema200 and f1h.supertrend_direction > 0
        if bullish
        else f1h.close < f1h.ema50 < f1h.ema200 and f1h.supertrend_direction < 0
    )
    if htf_aligned:
        score += 22
        reasons.append("1h тренд и Supertrend согласованы до импульса")
    else:
        blockers.append("Нет строгого подтверждения направления на 1h")

    h4_aligned = (
        f4h.close > f4h.ema50 and f4h.supertrend_direction > 0
        if bullish
        else f4h.close < f4h.ema50 and f4h.supertrend_direction < 0
    )
    h4_opposite = (
        f4h.close < f4h.ema50 and f4h.supertrend_direction < 0
        if bullish
        else f4h.close > f4h.ema50 and f4h.supertrend_direction > 0
    )
    if h4_aligned:
        score += 12
        reasons.append("4h не против сценария и подтверждает старший контекст")
    elif h4_opposite:
        blockers.append("4h направлен против раннего сценария")

    directional_recent_move = recent_move_15m_pct if bullish else -recent_move_15m_pct
    if directional_recent_move <= 0.15:
        score += 8
        reasons.append(f"За последние ~15м цена ещё не убежала: {recent_move_15m_pct:+.2f}%")
    elif directional_recent_move > settings.prime_max_directional_move_15m_pct:
        blockers.append(f"Цена уже прошла {directional_recent_move:.2f}% по сценарию за ~15м")

    aligned15 = f15.close > f15.ema20 > f15.ema50 if bullish else f15.close < f15.ema20 < f15.ema50
    if aligned15:
        score += 14
        reasons.append("15m структура уже направлена, но BOS ещё не состоялся")

    directional_dmi = f15.dmi_spread if bullish else -f15.dmi_spread
    if directional_dmi >= 6:
        score += 6

    trigger = f15.range_high20 if bullish else f15.range_low20
    distance_pct = abs(f15.close / trigger - 1) * 100 if trigger > 0 else float("inf")
    if (
        settings.prime_min_trigger_distance_pct
        <= distance_pct
        <= settings.prime_max_trigger_distance_pct
    ):
        score += 20
        reasons.append(f"До trigger {distance_pct:.2f}%: есть запас до движения")
    elif distance_pct < settings.prime_min_trigger_distance_pct:
        blockers.append(f"До trigger всего {distance_pct:.2f}% — сигнал уже слишком поздний")
    else:
        blockers.append(f"Trigger слишком далеко: {distance_pct:.2f}%")

    if f15.keltner_squeeze_ratio <= 1.05:
        score += 12
        reasons.append("Волатильность сжата: рынок ещё не разогнан")
    elif f15.keltner_squeeze_ratio <= 1.15:
        score += 5

    if f15.bb_width_regime_ratio <= 0.95:
        score += 6

    rvol = f15.relative_volume20
    if 0.65 <= rvol <= 1.25:
        score += 8
        reasons.append(f"RVOL {rvol:.2f}×: объём ещё не перешёл в импульс")
    elif 1.25 < rvol <= 1.50:
        score += 3
    elif rvol > 1.50:
        blockers.append(f"RVOL {rvol:.2f}×: активный поток уже начался")

    oi = ticker.open_interest_change_pct
    if oi is not None:
        if 0 <= oi <= 5:
            score += 8
            reasons.append(f"OI {oi:+.1f}%: позиции набираются без экстремума")
        elif oi < -1:
            blockers.append(f"OI {oi:+.1f}%: позиции сокращаются")

    if settings.prime_spot_confirmation_enabled:
        spot_evidence = 0
        spot_taker = ticker.spot_taker_buy_ratio
        if spot_taker is not None:
            directional_spot_taker = spot_taker if bullish else 1 - spot_taker
            if directional_spot_taker >= settings.prime_min_spot_taker_ratio:
                score += 10
                spot_evidence += 1
            elif directional_spot_taker <= 1 - settings.prime_min_spot_taker_ratio:
                blockers.append(f"Spot taker-flow {directional_spot_taker:.0%} против сценария")

        spot_book = ticker.spot_orderbook_imbalance
        if spot_book is not None:
            directional_spot_book = spot_book if bullish else -spot_book
            if directional_spot_book >= settings.prime_min_spot_book_imbalance:
                score += 6
                spot_evidence += 1
            elif directional_spot_book <= -0.12:
                blockers.append("Снимок spot-стакана против сценария")

        block_ratio = ticker.spot_block_trade_buy_ratio
        block_notional = ticker.spot_block_trade_notional
        if block_ratio is not None and block_notional is not None:
            directional_block_ratio = block_ratio if bullish else 1 - block_ratio
            if (
                block_notional >= settings.prime_min_spot_block_notional
                and directional_block_ratio >= 0.65
            ):
                score += 8
                spot_evidence += 1
                reasons.append(
                    f"Spot block trades ${block_notional:,.0f}: "
                    f"{directional_block_ratio:.0%} по сценарию"
                )

        basis_bps = ticker.spot_perp_basis_bps
        if basis_bps is not None:
            directional_basis = basis_bps if bullish else -basis_bps
            if directional_basis <= settings.prime_max_directional_perp_basis_bps:
                score += 4
            else:
                blockers.append(
                    f"Perp уже перегрет относительно spot: {directional_basis:+.1f} bps"
                )

        spot_data_present = any(
            value is not None
            for value in (
                spot_taker,
                spot_book,
                block_ratio,
                ticker.spot_perp_basis_bps,
            )
        )
        if spot_evidence >= settings.prime_min_spot_confirmations:
            reasons.append(
                "Spot-прокси согласованы со сценарием; это не доказательство опережения perp"
            )
        elif spot_data_present:
            blockers.append(
                f"Spot дал только {spot_evidence}/"
                f"{settings.prime_min_spot_confirmations} подтверждений-прокси"
            )
        else:
            blockers.append("Spot-данные недоступны: обязательное подтверждение не проверено")

    funding_pct = ticker.funding_rate * 100
    directional_funding = funding_pct if bullish else -funding_pct
    if directional_funding <= 0.05:
        score += 5
    elif directional_funding > settings.flow_max_directional_funding_pct:
        blockers.append(f"Funding {funding_pct:+.3f}% уже перегрет")

    if abs(f15.vwap_distance_atr) <= 1.5:
        score += 5
    elif abs(f15.vwap_distance_atr) > 2.2:
        blockers.append("Цена уже слишком далеко от VWAP")

    if flow is not None and flow.fresh:
        price_move = abs(flow.price_change_60s_pct or 0.0)
        if price_move <= settings.prime_max_price_move_60s_pct:
            score += 8
            reasons.append(f"Цена за 60с почти не ушла: {price_move:.2f}%")
        else:
            blockers.append(f"Цена уже ускорилась на {price_move:.2f}% за 60с")

        burst = flow.volume_burst_ratio
        if burst is None or burst <= 1.25:
            score += 5
        elif burst >= 1.50:
            blockers.append(f"Поток уже ускорился до {burst:.2f}×")

        if abs(flow.delta_ratio_60s) <= 0.30:
            score += 3
        elif abs(flow.delta_ratio_60s) >= 0.55:
            blockers.append("Taker delta уже экстремальна — это не pre-flow")

        accel = flow.oi_acceleration_pct_per_min
        if accel is not None and 0.02 <= accel <= 0.12:
            score += 5
            reasons.append(f"OI начинает ускоряться: {accel:+.3f}%/мин")
        elif accel is not None and accel > 0.20:
            blockers.append("OI уже ускоряется слишком резко")

        matching_absorption = (
            flow.absorption == "BUY_ABSORPTION" if bullish else flow.absorption == "SELL_ABSORPTION"
        )
        if matching_absorption and price_move <= settings.prime_max_price_move_60s_pct:
            score += 6
            reasons.append("Есть раннее поглощение без заметного движения цены")

    if settings.liquidity_intelligence_enabled and liquidity is not None:
        directional_wall = liquidity.bid_wall_ratio if bullish else liquidity.ask_wall_ratio
        opposing_wall = liquidity.ask_wall_ratio if bullish else liquidity.bid_wall_ratio
        directional_persistence = (
            liquidity.bid_wall_persistence_seconds
            if bullish
            else liquidity.ask_wall_persistence_seconds
        )
        opposing_persistence = (
            liquidity.ask_wall_persistence_seconds
            if bullish
            else liquidity.bid_wall_persistence_seconds
        )
        directional_replenishment = (
            liquidity.bid_replenishment_usdt_60s
            if bullish
            else liquidity.ask_replenishment_usdt_60s
        )
        opposing_replenishment = (
            liquidity.ask_replenishment_usdt_60s
            if bullish
            else liquidity.bid_replenishment_usdt_60s
        )

        persistent_support = (
            directional_wall is not None
            and directional_wall >= settings.prime_min_persistent_wall_ratio
            and directional_persistence >= settings.prime_min_wall_persistence_seconds
            and directional_replenishment >= settings.prime_min_replenishment_notional
        )
        persistent_resistance = (
            opposing_wall is not None
            and opposing_wall >= settings.prime_min_persistent_wall_ratio * 1.3
            and opposing_persistence >= settings.prime_min_wall_persistence_seconds
            and opposing_replenishment
            > max(
                directional_replenishment * 1.5,
                settings.prime_min_replenishment_notional,
            )
        )
        if persistent_support:
            score += 9
            reasons.append("Стакан не просто показывает стену: уровень держится и пополняется")
        if persistent_resistance:
            blockers.append(
                "Против сценария стоит более сильная устойчивая и пополняемая ликвидность"
            )

        trade_notional = flow.notional_60s if flow is not None and flow.fresh else 0.0
        if trade_notional > 0:
            squeeze_liquidation = (
                liquidity.short_liquidation_usdt_60s
                if bullish
                else liquidity.long_liquidation_usdt_60s
            )
            flush_liquidation = (
                liquidity.long_liquidation_usdt_60s
                if bullish
                else liquidity.short_liquidation_usdt_60s
            )
            squeeze_ratio = squeeze_liquidation / trade_notional
            flush_ratio = flush_liquidation / trade_notional

            if squeeze_ratio > settings.prime_max_directional_liquidation_ratio:
                blockers.append(
                    f"Движение уже подпитывают ликвидации ({squeeze_ratio:.0%} потока) — "
                    "риск позднего входа"
                )
            elif (
                flush_ratio >= 0.12
                and abs(flow.price_change_60s_pct or 0.0) <= settings.prime_max_price_move_60s_pct
            ):
                score += 5
                reasons.append(
                    "Противоположные ликвидации прошли, но цена удержалась — возможное поглощение"
                )

    return int(round(max(0.0, min(score, 100.0)))), reasons, blockers


def _live_flow_adjustment(
    side: Side,
    flow: FlowSnapshot,
) -> tuple[float, list[str], list[str]]:
    bullish = side is Side.LONG
    direction = 1.0 if bullish else -1.0
    directional_delta = direction * flow.delta_ratio_60s
    directional_cvd = direction * flow.cvd_ratio_5m
    score = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    matching_absorption = (
        flow.absorption == "BUY_ABSORPTION" if bullish else flow.absorption == "SELL_ABSORPTION"
    )
    opposing_absorption = (
        flow.absorption == "SELL_ABSORPTION" if bullish else flow.absorption == "BUY_ABSORPTION"
    )

    if directional_delta >= 0.18:
        score += 10
        reasons.append(f"Live delta 60s {directional_delta:+.0%} поддерживает {side.value}")
    elif directional_delta <= -0.22 and not matching_absorption:
        score -= 8
        warnings.append("Live taker delta сейчас против сценария")

    if directional_cvd >= 0.10:
        score += 7
        reasons.append(f"Live CVD 5m {directional_cvd:+.0%} подтверждает поток")
    elif directional_cvd <= -0.15:
        score -= 5
        warnings.append("5m CVD proxy расходится с направлением")

    burst = flow.volume_burst_ratio
    if burst is not None and burst >= 1.5:
        score += 5
        reasons.append(f"Поток сделок ускорился до {burst:.2f}× к прошлой минуте")

    oi_change = flow.oi_change_2m_pct
    if oi_change is not None:
        if oi_change >= 0.10:
            score += 5
            reasons.append(f"Live OI +{oi_change:.2f}% за ~2 мин")
        elif oi_change <= -0.20:
            score -= 4
            warnings.append(f"Live OI {oi_change:+.2f}%: позиции сокращаются")

    acceleration = flow.oi_acceleration_pct_per_min
    if acceleration is not None and acceleration >= 0.03:
        score += 3
        reasons.append(f"Ускорение OI {acceleration:+.3f}%/мин")

    matching_absorption = (
        flow.absorption == "BUY_ABSORPTION" if bullish else flow.absorption == "SELL_ABSORPTION"
    )
    opposing_absorption = (
        flow.absorption == "SELL_ABSORPTION" if bullish else flow.absorption == "BUY_ABSORPTION"
    )
    if matching_absorption:
        score += 8
        reasons.append("Поток поглощается у цены — возможен пассивный набор до BOS")
    elif opposing_absorption:
        score -= 8
        warnings.append("Обнаружено поглощение против выбранного направления")

    return score, reasons, warnings


def _stage(
    side: Side,
    score: int,
    f15: FeatureSet,
    f1h: FeatureSet,
    ticker: Ticker,
    flow: FlowSnapshot | None = None,
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
        taker_ok = False
    elif bullish:
        taker_ok = ticker.taker_buy_ratio >= 0.54
    else:
        taker_ok = ticker.taker_buy_ratio <= 0.46
    oi_ok = ticker.open_interest_change_pct is not None and ticker.open_interest_change_pct >= 0
    flow_conflict = False
    live_prepressure = False
    if flow is not None and flow.fresh:
        direction = 1.0 if bullish else -1.0
        directional_delta = direction * flow.delta_ratio_60s
        directional_cvd = direction * flow.cvd_ratio_5m
        flow_conflict = directional_delta <= -0.22 and directional_cvd <= -0.12
        burst_ok = flow.volume_burst_ratio is not None and flow.volume_burst_ratio >= 1.2
        oi_live_ok = flow.oi_change_2m_pct is None or flow.oi_change_2m_pct >= 0
        matching_absorption = (
            flow.absorption == "BUY_ABSORPTION" if bullish else flow.absorption == "SELL_ABSORPTION"
        )
        live_prepressure = (
            (directional_delta >= 0.12 or matching_absorption) and burst_ok and oi_live_ok
        )
    if (
        score >= 78
        and breakout
        and f15.relative_volume20 >= 1.2
        and taker_ok
        and oi_ok
        and htf
        and not flow_conflict
    ):
        return "ENTRY"
    if score >= 72 and htf and (near_level or live_prepressure):
        return "ARMED"
    return "WATCH"


def _recent_close_move_pct(candles, bars: int) -> float:
    if len(candles) <= bars:
        return 0.0
    start = candles[-(bars + 1)].close
    end = candles[-1].close
    if start <= 0:
        return 0.0
    return (end / start - 1) * 100


def _structure_label(feature: FeatureSet) -> str:
    if feature.breakout_up:
        return "Пробой диапазона 20 свечей ↑"
    if feature.breakout_down:
        return "Пробой диапазона 20 свечей ↓"
    if feature.close > feature.ema20 > feature.ema50:
        return "EMA20/50 ↑ (экстремумы не проверены)"
    if feature.close < feature.ema20 < feature.ema50:
        return "EMA20/50 ↓ (экстремумы не проверены)"
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
    armed = sum(item.stage == "ARMED" for item in report.setups)
    watching = sum(item.stage == "WATCH" for item in report.setups)
    prime = sum(item.prime_ready for item in report.setups)
    return (
        "<b>🐋 Smart Money Radar · PRE-MOVE</b>\n"
        f"Биржа: {html.escape(report.exchange)} · рынок: {report.universe_count} · "
        f"глубокий анализ: {report.analyzed_count}\n"
        f"PRIME-ready: {prime} · ARMED: {armed} · WATCH: {watching} · "
        f"показано: {len(report.setups)}\n\n"
        "Пробитые/уже разогнанные ENTRY-сценарии по умолчанию скрыты. "
        "Радар ищет признаки подготовки по публичным данным, а не идентифицирует конкретных китов."
    )


def format_smart_money_setup(item: SmartMoneySetup) -> str:
    stage_icon = {"ENTRY": "⚪", "ARMED": "🟡", "WATCH": "🔵"}.get(item.stage, "⚪")
    side_icon = "↗️" if item.bias is Side.LONG else "↘️"
    oi = "н/д" if item.oi_change_pct is None else f"{item.oi_change_pct:+.1f}%"
    taker = "н/д" if item.taker_buy_ratio is None else f"{item.taker_buy_ratio:.0%} buy"
    book = "н/д" if item.orderbook_imbalance is None else f"{item.orderbook_imbalance:+.0%}"
    reasons = "\n".join(f"• {html.escape(value)}" for value in item.reasons)
    warnings = "\n".join(f"• {html.escape(value)}" for value in item.warnings)
    live_parts: list[str] = []
    if item.live_delta_ratio_60s is not None:
        live_parts.append(f"Δ60s {item.live_delta_ratio_60s:+.0%}")
    if item.live_cvd_ratio_5m is not None:
        live_parts.append(f"CVD5m {item.live_cvd_ratio_5m:+.0%}")
    if item.live_volume_burst_ratio is not None:
        live_parts.append(f"burst {item.live_volume_burst_ratio:.2f}×")
    if item.live_oi_change_2m_pct is not None:
        live_parts.append(f"OI2m {item.live_oi_change_2m_pct:+.2f}%")
    if item.live_oi_acceleration_pct_per_min is not None:
        live_parts.append(f"OI accel {item.live_oi_acceleration_pct_per_min:+.3f}%/мин")
    if item.live_absorption:
        live_parts.append(item.live_absorption)
    live_line = " · ".join(live_parts)
    stage_text = {
        "ENTRY": "пробой уже произошёл; это позднее подтверждение, а не новый вход",
        "ARMED": "сетап поджат к trigger; ищем вход до основного ускорения",
        "WATCH": "раннее наблюдение; ждём усиления факторов без погони за ценой",
    }.get(item.stage, "наблюдение")
    return (
        f"{stage_icon} <b>{html.escape(item.symbol)} · {item.stage} · "
        f"{item.bias.value}</b> {side_icon}\n"
        f"Score: <b>{item.score}/100</b> · цена: <code>{_price(item.price)}</code>\n"
        f"Структура: 15m {item.structure_15m} · 1h {item.structure_1h}\n"
        f"RVOL: {item.rvol:.2f}× · OI: {oi} · taker: {taker}\n"
        f"Book: {book} · funding: {item.funding_pct:+.3f}%\n"
        + (f"Live flow: {html.escape(live_line)}\n" if live_line else "")
        + f"Trigger: <code>{_price(item.trigger_price)}</code> · "
        f"invalidation: <code>{_price(item.invalidation_price)}</code>\n\n"
        f"<b>Почему в списке</b>\n{reasons or '• Совпало несколько независимых факторов'}\n"
        + (f"\n<b>Риски</b>\n{warnings}\n" if warnings else "")
        + f"\n<b>Стадия:</b> {stage_text}.\n"
        "⚠️ Taker-flow здесь — прокси дельты из публичных сделок/агрегатов; "
        "это не биржевой footprint-CVD и не доказательство входа институционала."
    )
