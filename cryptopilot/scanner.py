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
        benchmark, *series = await asyncio.gather(
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
        await self.store.save(signal)
        return signal

    async def auto_candidates(self) -> list[Signal]:
        report = await self.scan_market()
        selected: list[Signal] = []
        for signal in report.signals:
            if signal.confidence < self.settings.min_auto_confidence:
                continue
            if signal.plan is None or signal.plan.risk_reward_2 < self.settings.min_risk_reward:
                continue
            if await self.store.should_alert(signal, self.settings.alert_cooldown_minutes):
                selected.append(signal)
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
            for signal in await self.auto_candidates():
                await callback(signal)
                await self.store.mark_alerted(signal)
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
        fetched = await asyncio.gather(
            *(self.exchange.candles(symbol, timeframe, 260) for timeframe in missing)
        )
        all_series = {cached_tf: cached}
        all_series.update(dict(zip(missing, fetched, strict=True)))
        return self.engine.analyze(
            symbol, self.exchange.name, ticker, all_series, benchmark or None
        )
