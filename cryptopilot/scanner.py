from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import aiohttp

from cryptopilot.config import Settings
from cryptopilot.engine import SignalEngine
from cryptopilot.exchange import ExchangeClient, MarketDataError
from cryptopilot.models import Candle, ScanReport, Signal, Ticker
from cryptopilot.paper import PaperTracker
from cryptopilot.storage import SignalStore

log = logging.getLogger(__name__)
AlertCallback = Callable[[Signal], Awaitable[None]]


class MarketScanner:
    def __init__(
        self,
        exchange: ExchangeClient,
        engine: SignalEngine,
        store: SignalStore,
        settings: Settings,
    ) -> None:
        self.exchange = exchange
        self.engine = engine
        self.store = store
        self.settings = settings
        self.scan_lock = asyncio.Lock()
        self.last_report: ScanReport | None = None
        self.last_error: str | None = None
        self.paper = PaperTracker(exchange, store, settings)

    async def scan_market(self) -> ScanReport:
        async with self.scan_lock:
            started = datetime.now(UTC)
            errors: list[str] = []
            tickers = await self.exchange.tickers()
            universe = self._universe(tickers)
            ticker_map = {item.symbol: item for item in universe}
            quick_tf = self.settings.timeframe_list[1]

            quick_results = await asyncio.gather(
                *(self._quick(item.symbol, quick_tf) for item in universe), return_exceptions=True
            )
            ranked: list[tuple[float, str, list[Candle]]] = []
            for ticker, result in zip(universe, quick_results, strict=True):
                if isinstance(result, BaseException):
                    errors.append(f"{ticker.symbol}: {type(result).__name__}")
                    continue
                score, series = result
                ranked.append((abs(score), ticker.symbol, series))
            ranked.sort(reverse=True)
            candidates = ranked[: self.settings.shortlist_size]

            try:
                benchmark = await self.exchange.candles(
                    "BTCUSDT", self.settings.timeframe_list[-1], 260
                )
            except Exception as exc:  # scanner must still produce conservative NO TRADE results
                log.warning("BTC benchmark unavailable: %s", exc)
                benchmark = []
                errors.append("BTC benchmark unavailable")

            analyses = await asyncio.gather(
                *(
                    self._analyze_with_cached(
                        symbol, ticker_map[symbol], quick_tf, cached, benchmark
                    )
                    for _, symbol, cached in candidates
                ),
                return_exceptions=True,
            )
            signals: list[Signal] = []
            for candidate, result in zip(candidates, analyses, strict=True):
                symbol = candidate[1]
                if isinstance(result, BaseException):
                    errors.append(f"{symbol}: {type(result).__name__}")
                    log.warning("Detailed analysis failed for %s: %s", symbol, result)
                    continue
                if result.actionable:
                    await self._attach_calibration(result)
                    await self.store.save(result)
                    signals.append(result)

            signals.sort(key=lambda item: (item.confidence, abs(item.score)), reverse=True)
            report = ScanReport(
                exchange=self.exchange.name,
                started_at=started,
                finished_at=datetime.now(UTC),
                universe_count=len(universe),
                analyzed_count=len(candidates),
                signals=tuple(signals),
                errors=tuple(errors[:12]),
            )
            self.last_report = report
            self.last_error = None
            await self.store.set_runtime("last_scan", report.finished_at.isoformat())
            return report

    async def analyze_symbol(self, symbol: str) -> Signal:
        normalized = symbol.upper().replace("/", "").replace("-", "")
        if not normalized.endswith("USDT"):
            normalized += "USDT"
        tickers = await self.exchange.tickers()
        ticker = next((item for item in tickers if item.symbol == normalized), None)
        if ticker is None:
            raise ValueError(
                f"{normalized} is not an active USDT perpetual on {self.exchange.name}"
            )
        ticker, benchmark, *series = await asyncio.gather(
            self.exchange.enrich_ticker(ticker),
            self.exchange.candles("BTCUSDT", self.settings.timeframe_list[-1], 260),
            *(self.exchange.candles(normalized, tf, 260) for tf in self.settings.timeframe_list),
        )
        signal = self.engine.analyze(
            normalized,
            self.exchange.name,
            ticker,
            dict(zip(self.settings.timeframe_list, series, strict=True)),
            benchmark,
        )
        if signal.actionable:
            await self._attach_calibration(signal)
        await self.store.save(signal)
        return signal

    async def auto_candidates(self) -> list[Signal]:
        report = await self.scan_market()
        selected: list[Signal] = []
        active = (
            await self.store.active_paper_count()
            if self.settings.paper_tracking_enabled
            else 0
        )
        risk_slots = max(
            0,
            int(self.settings.max_portfolio_risk_pct / self.settings.risk_per_trade_pct)
            - active,
        )
        limit = min(self.settings.max_auto_signals_per_scan, risk_slots)
        side_counts: dict[str, int] = {}
        for signal in report.signals:
            if len(selected) >= limit:
                break
            if signal.confidence < signal.required_confidence:
                continue
            if signal.plan is None or signal.plan.risk_reward_2 < self.settings.min_risk_reward:
                continue
            side_key = signal.side.value
            if side_counts.get(side_key, 0) >= self.settings.max_same_side_auto_signals:
                continue
            if await self.store.should_alert(signal, self.settings.alert_cooldown_minutes):
                selected.append(signal)
                side_counts[side_key] = side_counts.get(side_key, 0) + 1
        return selected

    async def monitor(self, callback: AlertCallback, stop_event: asyncio.Event) -> None:
        if self.settings.run_scan_on_startup:
            await self._monitor_once(callback)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.scan_interval_seconds
                )
            except TimeoutError:
                await self._monitor_once(callback)

    async def _monitor_once(self, callback: AlertCallback) -> None:
        try:
            refresh = await self.paper.refresh()
            if refresh.closed or refresh.expired:
                log.info(
                    "Paper tracker: reviewed=%d entered=%d closed=%d expired=%d errors=%d",
                    refresh.reviewed,
                    refresh.entered,
                    refresh.closed,
                    refresh.expired,
                    refresh.errors,
                )
            for signal in await self.auto_candidates():
                await callback(signal)
                await self.store.mark_alerted(
                    signal,
                    track_paper=self.settings.paper_tracking_enabled,
                    max_holding_hours=self.settings.paper_max_holding_hours,
                )
        except (MarketDataError, aiohttp.ClientError) as exc:
            self.last_error = str(exc)
            log.exception("Automatic market scan failed")
        except Exception as exc:
            self.last_error = str(exc)
            log.exception("Unexpected automatic scan failure")

    def _universe(self, tickers: list[Ticker]) -> list[Ticker]:
        filtered = [
            item
            for item in tickers
            if item.symbol not in self.settings.excluded_symbol_set
            and item.turnover_24h >= self.settings.min_volume_usdt
            and item.last > 0
            and item.bid > 0
            and item.ask >= item.bid
            and item.spread_bps <= self.settings.max_spread_bps
        ]
        filtered.sort(key=lambda item: item.turnover_24h, reverse=True)
        return filtered[: self.settings.universe_size]

    async def _quick(self, symbol: str, timeframe: str) -> tuple[float, list[Candle]]:
        series = await self.exchange.candles(symbol, timeframe, 240)
        return self.engine.quick_score(series), series

    async def _analyze_with_cached(
        self,
        symbol: str,
        ticker: Ticker,
        cached_tf: str,
        cached: list[Candle],
        benchmark: list[Candle],
    ) -> Signal:
        missing = [tf for tf in self.settings.timeframe_list if tf != cached_tf]
        enriched, *fetched = await asyncio.gather(
            self.exchange.enrich_ticker(ticker),
            *(self.exchange.candles(symbol, timeframe, 260) for timeframe in missing),
        )
        all_series = {cached_tf: cached}
        all_series.update(dict(zip(missing, fetched, strict=True)))
        return self.engine.analyze(
            symbol, self.exchange.name, enriched, all_series, benchmark or None
        )

    async def _attach_calibration(self, signal: Signal) -> None:
        specific = await self.store.calibration(
            symbol=signal.symbol,
            side=signal.side,
            limit=self.settings.calibration_lookback,
        )
        chosen = specific
        if specific.sample_size < self.settings.calibration_min_samples:
            side_stats = await self.store.calibration(
                side=signal.side,
                limit=self.settings.calibration_lookback,
            )
            if side_stats.sample_size > specific.sample_size:
                chosen = side_stats
        signal.calibration_samples = chosen.sample_size
        signal.recent_expectancy_r = (
            chosen.expectancy_r if chosen.sample_size else None
        )
        if chosen.sample_size:
            signal.estimated_success_pct = chosen.win_rate
            signal.success_interval_low = chosen.interval_low
            signal.success_interval_high = chosen.interval_high
        if (
            chosen.sample_size >= self.settings.calibration_min_samples
            and chosen.expectancy_r <= 0
        ):
            signal.required_confidence = min(
                95,
                signal.required_confidence + self.settings.weak_edge_confidence_penalty,
            )
