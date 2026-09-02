"""
app/services/scanner.py — прикладной сервис сканирования рынка.

Связывает слои: данные → скрининг → движок анализа → отчёт. Telegram-слой
вызывает только его и ничего не знает про биржи и индикаторы.

Этапы прогона (с прогресс-колбэком для интерфейса):
  1. тикеры всей вселенной                     (1 запрос к бирже)
  2. грубый отбор по тикерам                   (бесплатно)
  3. свечи базового ТФ для выживших            (по 1 запросу на монету)
  4. тонкий отбор «сжатой пружины»             (бесплатно)
  5. полный снимок для топа: дневки + funding/OI
  6. глубокий анализ каждого кандидата
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from app.config.settings import Settings
from app.data.market import MarketDataService
from app.domain.models import (Direction, MarketContext, MarketSnapshot,
                               PrescreenCandidate, ScanReport, Signal)
from app.screening.prescreen import PrescreenConfig, coarse_screen, fine_screen
from app.signals.engine import SignalEngine

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float], None]


class ScannerService:
    """Оркестратор одного прогона сканирования + кэш последнего результата."""

    def __init__(self, settings: Settings, market: MarketDataService,
                 engine: Optional[SignalEngine] = None) -> None:
        self.settings = settings
        self.market = market
        self.engine = engine or SignalEngine(settings)
        self._last_report: Optional[ScanReport] = None
        self._lock = asyncio.Lock()
        self._scanning = False

    # ------------------------------------------------------------------
    @property
    def last_report(self) -> Optional[ScanReport]:
        return self._last_report

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    # ------------------------------------------------------------------
    async def run_scan(self, progress: Optional[ProgressCallback] = None
                       ) -> ScanReport:
        """Полный прогон. Одновременно может идти только один (защита от гонок)."""
        if self._scanning:
            raise RuntimeError("сканирование уже идёт")
        async with self._lock:
            self._scanning = True
            started = time.monotonic()
            report = ScanReport()
            try:
                report = await self._scan(progress, report)
            except Exception as exc:  # noqa: BLE001
                log.exception("прогон сканера упал: %s", exc)
                report.errors.append(f"ошибка сканирования: {exc}")
            finally:
                report.duration_seconds = round(time.monotonic() - started, 2)
                report.finished_at = datetime.now(timezone.utc)
                self._last_report = report
                self._scanning = False
            return report

    # ------------------------------------------------------------------
    async def _scan(self, progress: Optional[ProgressCallback],
                    report: ScanReport) -> ScanReport:
        cfg = PrescreenConfig(
            min_quote_volume=self.settings.min_quote_volume_usd,
            max_change_z=self.settings.max_run_zscore,
            coarse_candidates=max(60, self.settings.prescreen_candidates * 3),
            fine_candidates=self.settings.prescreen_candidates,
            volume_anomaly_z=self.settings.volume_anomaly_z,
        )

        _progress(progress, "Загружаю тикеры рынка…", 0.05)
        tickers = await self.market.tickers()
        if not tickers:
            report.errors.append("не удалось получить тикеры: биржи недоступны")
            return report
        stats = self.market.universe_stats(tickers)

        _progress(progress, "Оцениваю фон рынка (BTC, ширина)…", 0.12)
        context = await self.market.market_context(tickers)
        report.context = context

        universe = await self.market.universe()
        report.universe_size = len(universe)
        if not universe:
            report.errors.append("вселенная пуста: проверьте MIN_QUOTE_VOLUME_USD")
            return report
        universe_set = set(universe)
        filtered = {s: t for s, t in tickers.items() if s in universe_set}

        _progress(progress, f"Грубый отбор из {len(filtered)} монет…", 0.2)
        coarse = coarse_screen(filtered, stats, cfg)
        report.candidates = coarse[: self.settings.prescreen_candidates]
        if not coarse:
            report.errors.append("грубый отбор не дал кандидатов (рынок слишком узкий?)")
            return report

        _progress(progress, f"Загружаю свечи {len(coarse)} кандидатов…", 0.35)
        light = await self._light_snapshots([c.symbol for c in coarse])

        _progress(progress, "Тонкий отбор «сжатой пружины»…", 0.55)
        fine = fine_screen(light, cfg, signal_tf=self.settings.base_timeframe)
        if fine:
            report.candidates = fine
        symbols = [c.symbol for c in (fine or coarse[: self.settings.prescreen_candidates])]

        _progress(progress, f"Глубокий анализ {len(symbols)} монет…", 0.65)
        snapshots = await self.market.snapshots(symbols)
        report.scanned = len(snapshots)

        signals: List[Signal] = []
        done = 0
        for snapshot in snapshots:
            try:
                signals.append(await asyncio.to_thread(
                    self.engine.analyze, snapshot, context))
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: анализ упал: %s", snapshot.symbol, exc)
                report.errors.append(f"{snapshot.symbol}: {exc}")
            done += 1
            if done % 5 == 0:
                _progress(progress, f"Глубокий анализ {done}/{len(snapshots)}…",
                          0.65 + 0.3 * done / max(1, len(snapshots)))

        signals.sort(key=lambda s: (-int(s.actionable), -s.confidence))
        report.signals = signals
        _progress(progress, "Готово", 1.0)
        return report

    # ------------------------------------------------------------------
    async def _light_snapshots(self, symbols: Sequence[str]
                               ) -> List[MarketSnapshot]:
        """Только свечи базового ТФ + тикер: дёшево, для тонкого отбора."""
        tickers = await self.market.tickers()
        stats = self.market.universe_stats(tickers)
        sem = asyncio.Semaphore(max(2, self.settings.rate_limit_concurrency * 2))

        async def _one(symbol: str) -> Optional[MarketSnapshot]:
            async with sem:
                candles = await self.market.candles(
                    symbol, self.settings.base_timeframe, self.settings.bars_base)
                if candles is None or candles.empty:
                    return None
                base, quote = symbol.split("/")[:2]
                return MarketSnapshot(
                    symbol=symbol, base=base, quote=quote,
                    exchange=candles.source or "",
                    candles={self.settings.base_timeframe: candles},
                    ticker=tickers.get(symbol), universe_stats=stats,
                )

        results = await asyncio.gather(*[_one(s) for s in symbols],
                                       return_exceptions=True)
        return [r for r in results if isinstance(r, MarketSnapshot)]

    # ------------------------------------------------------------------
    async def analyze_symbol(self, symbol: str) -> Signal:
        """Глубокий анализ одной монеты по запросу пользователя."""
        symbol = normalize_symbol(symbol, self.settings.quote)
        tickers = await self.market.tickers()
        stats = self.market.universe_stats(tickers)
        snapshot = await self.market.snapshot(symbol, tickers, stats)
        if snapshot is None:
            raise ValueError(f"{symbol}: не удалось получить данные с биржи")
        context = await self.market.market_context(tickers)
        return await asyncio.to_thread(self.engine.analyze, snapshot, context)

    async def top_signals(self, limit: Optional[int] = None) -> ScanReport:
        """Последний прогон (или свежий, если его ещё не было)."""
        if self._last_report is None:
            return await self.run_scan()
        return self._last_report

    def stats(self) -> Dict[str, object]:
        report = self._last_report
        if report is None:
            return {"scans": 0}
        actionable = [s for s in report.signals if s.actionable]
        longs = [s for s in actionable if s.direction is Direction.LONG]
        shorts = [s for s in actionable if s.direction is Direction.SHORT]
        conf = [s.confidence for s in actionable]
        return {
            "finished_at": report.finished_at.isoformat(timespec="seconds"),
            "universe": report.universe_size,
            "scanned": report.scanned,
            "signals_total": len(report.signals),
            "actionable": len(actionable),
            "longs": len(longs),
            "shorts": len(shorts),
            "avg_confidence": round(sum(conf) / len(conf), 2) if conf else 0.0,
            "duration_s": report.duration_seconds,
            "errors": len(report.errors),
        }


def normalize_symbol(raw: str, quote: str) -> str:
    """'btc', 'BTCUSDT', 'BTC/USDT' → 'BTC/USDT'."""
    text = (raw or "").strip().upper().replace(" ", "")
    if "/" in text:
        base, q = text.split("/")[:2]
        return f"{base}/{q}"
    if text.endswith(quote):
        text = text[: -len(quote)]
    for stable in ("USDT", "USDC", "BUSD", "FDUSD", "TUSD"):
        if text.endswith(stable) and len(text) > len(stable):
            text = text[: -len(stable)]
            break
    return f"{text}/{quote}"


def _progress(cb: Optional[ProgressCallback], message: str, ratio: float) -> None:
    if cb is None:
        return
    try:
        cb(message, float(max(0.0, min(1.0, ratio))))
    except Exception:  # noqa: BLE001 — прогресс не должен ломать сканирование
        log.debug("progress callback failed", exc_info=True)
