"""Isolated forward-paper experiment. Never emits a trading recommendation."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import suppress
from pathlib import Path

import aiohttp
import aiosqlite

from cryptopilot.economics import net_reward_risk
from cryptopilot.exchange import INTERVAL_MS
from cryptopilot.indicators import compute_features, directional_score
from cryptopilot.lab_report import format_statistics

log = logging.getLogger(__name__)
VERSION = "squeeze-reclaim-forward-v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")


def closed_series(bars, interval, now_ms):
    duration = INTERVAL_MS[interval]
    rows = [b for b in bars if b.open_time_ms + duration <= now_ms]
    if len(rows) < 230 or any(
        b.open_time_ms - a.open_time_ms != duration for a, b in zip(rows, rows[1:], strict=False)
    ):
        raise ValueError("Insufficient or discontinuous closed candles")
    if not 0 <= now_ms - rows[-1].open_time_ms - duration <= duration + 90_000:
        raise ValueError("Stale candles")
    return rows


def detect(series, benchmark, now_ms):
    bars = {tf: closed_series(series[tf], tf, now_ms) for tf in ("15", "60", "240")}
    signal_ms = bars["15"][-1].open_time_ms + 900_000
    if not 0 <= now_ms - signal_ms <= 90_000:
        return None
    f = {tf: compute_features(rows) for tf, rows in bars.items()}
    btc = compute_features(closed_series(benchmark, "240", now_ms))
    previous = compute_features(bars["15"][:-1])
    side = 1 if directional_score(f["240"]) > 0 else -1
    if (
        side * directional_score(f["240"]) < 25
        or side * directional_score(f["60"]) < 25
        or f["60"].adx14 < 20
        or side * directional_score(btc) < -25
    ):
        return None
    execution = f["15"]
    prior_rsi = previous.rsi14 if side == 1 else 100 - previous.rsi14
    current_rsi = execution.rsi14 if side == 1 else 100 - execution.rsi14
    if (
        side * (previous.close - previous.ema20) > 0
        or side * (execution.close - execution.ema20) <= 0
        or not 35 <= prior_rsi <= 55
        or not 45 <= current_rsi <= 65
    ):
        return None
    atr = execution.atr14
    if (
        atr <= 0
        or bars["15"][-1].high - bars["15"][-1].low > 1.5 * atr
        or abs(execution.close - execution.ema20) > atr
    ):
        return None
    ratios = [compute_features(bars["15"][:-n]).keltner_squeeze_ratio for n in range(1, 5)]
    if not all(math.isfinite(x) for x in ratios) or min(ratios) >= 1:
        return None
    return {
        "side": side,
        "signal_ms": signal_ms,
        "close": execution.close,
        "atr": atr,
        "swing": min(b.low for b in bars["15"][-18:])
        if side == 1
        else max(b.high for b in bars["15"][-18:]),
        "squeeze_ratios": ratios,
        "rsi": current_rsi,
        "version": VERSION,
    }


def fill_plan(candidate, ticker, now_ms):
    side = candidate["side"]
    entry = ticker.ask if side == 1 else ticker.bid
    if not all(math.isfinite(x) and x > 0 for x in (entry, ticker.bid, ticker.ask)):
        return None
    if ticker.bid > ticker.ask or not 0 <= now_ms - candidate["signal_ms"] <= 90_000:
        return None
    if abs(entry - candidate["close"]) > 0.25 * candidate["atr"]:
        return None
    stop = (
        min(candidate["swing"], entry - 1.45 * candidate["atr"])
        if side == 1
        else max(candidate["swing"], entry + 1.45 * candidate["atr"])
    )
    risk = side * (entry - stop)
    target = entry + side * 2 * risk
    if not 0.0025 <= risk / entry <= 0.05:
        return None
    if net_reward_risk(side == 1, entry, stop, target, 6) < 1.8:
        return None
    return dict(
        candidate,
        entry=entry,
        stop=stop,
        target=target,
        entry_ms=now_ms,
        expires_ms=now_ms + 72 * 3600_000,
        cursor_ms=now_ms // 60000 * 60000,
        status="OPEN",
    )


def advance(position, bars, now_ms):
    """Consume contiguous closed minutes; ambiguous first minute is censored."""
    p = dict(position)
    for bar in bars:
        if bar.open_time_ms < p["cursor_ms"] or bar.open_time_ms + 60000 > now_ms:
            continue
        if bar.open_time_ms != p["cursor_ms"]:
            return dict(p, status="CENSORED_GAP")
        stop_hit = bar.low <= p["stop"] if p["side"] == 1 else bar.high >= p["stop"]
        tp_hit = bar.high >= p["target"] if p["side"] == 1 else bar.low <= p["target"]
        if bar.open_time_ms < p["entry_ms"] and (stop_hit or tp_hit):
            return dict(p, status="CENSORED_ENTRY_MINUTE")
        if bar.open_time_ms + 60000 > p["expires_ms"]:
            return dict(p, status="CENSORED_TIME_BOUNDARY")
        if stop_hit or tp_hit:
            price = (
                (min(p["stop"], bar.open) if p["side"] == 1 else max(p["stop"], bar.open))
                if stop_hit
                else p["target"]
            )
            risk = abs(p["entry"] - p["stop"])
            gross = p["side"] * (price - p["entry"]) / risk
            hours = (bar.open_time_ms + 60000 - p["entry_ms"]) / 3600_000
            funding = 0.0001 * hours / 8 * p["entry"] / risk
            return dict(
                p,
                status="CLOSED",
                outcome="SL" if stop_hit else "TP2",
                exit_price=price,
                closed_ms=bar.open_time_ms + 60000,
                net_r=gross - 0.0006 * (p["entry"] + price) / risk - funding,
                stress_r=gross - 0.0012 * (p["entry"] + price) / risk - funding,
            )
        p["cursor_ms"] += 60000
    return p


class SqueezeLab:
    def __init__(self, exchange, store, settings):
        self.exchange, self.store, self.settings = exchange, store, settings
        self.path = Path(store.path)
        self.status = "starting"

    async def initialize(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS squeeze_lab ("
                "key TEXT PRIMARY KEY, symbol TEXT, payload TEXT NOT NULL)"
            )
            await db.commit()

    async def rows(self):
        async with aiosqlite.connect(self.path) as db:
            return await (await db.execute("SELECT key,symbol,payload FROM squeeze_lab")).fetchall()

    async def save(self, key, symbol, payload):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO squeeze_lab VALUES (?,?,?) ON CONFLICT(key) "
                "DO UPDATE SET payload=excluded.payload",
                (key, symbol, json.dumps(payload)),
            )
            await db.commit()

    async def cycle(self):
        rows = await self.rows()
        busy = set()
        for key, symbol, raw in rows:
            p = json.loads(raw)
            if p["status"] == "OPEN":
                bars = await self.exchange.candles(symbol, "1", 1000)
                p = advance(p, bars, int(time.time() * 1000))
                await self.save(key, symbol, p)
                busy.add(symbol)  # no immediate re-entry after a closure
            elif int(time.time() * 1000) < p.get("closed_ms", p["entry_ms"]) + 3600_000:
                busy.add(symbol)
        now = int(time.time() * 1000)
        # Only evaluate near the close, never hunt historical missed entries.
        if now % 900_000 > 90_000:
            return
        benchmark = await self.exchange.candles("BTCUSDT", "240", 260)
        known = {key for key, _, _ in rows}
        for symbol in SYMBOLS:
            key = f"{VERSION}:{self.exchange.name}:{symbol}:{now // 900000}"
            if symbol in busy or key in known:
                continue
            series = {
                tf: await self.exchange.candles(symbol, tf, 260) for tf in ("15", "60", "240")
            }
            candidate = await asyncio.to_thread(detect, series, benchmark, int(time.time() * 1000))
            if candidate is None:
                continue
            requested = time.monotonic()
            tickers = await self.exchange.tickers()
            received = int(time.time() * 1000)
            ticker = next((t for t in tickers if t.symbol == symbol), None)
            if ticker is None or time.monotonic() - requested > 5:
                continue
            if (
                ticker.spread_bps > self.settings.max_spread_bps
                or ticker.turnover_24h < self.settings.min_volume_usdt
            ):
                continue
            plan = fill_plan(candidate, ticker, received)
            if plan:
                await self.save(key, symbol, plan)

    async def run(self, stop):
        await self.initialize()
        while not stop.is_set():
            try:
                await self.cycle()
                self.status = "collecting"
                await self.store.set_runtime("squeeze_lab_heartbeat", str(int(time.time())))
            except aiohttp.ClientResponseError as exc:
                if exc.status in (401, 403, 418, 429):
                    self.status = "paused: exchange access/rate limit"
                    await stop.wait()
                    return
                self.status = "data error"
                log.warning("Squeeze lab HTTP data error")
            except Exception:
                self.status = "data error"
                log.exception("Squeeze lab cycle failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=60)

    async def report(self):
        records = [json.loads(raw) for _, _, raw in await self.rows()]
        return (
            "Лаборатория сжатия · только виртуальный эксперимент\n"
            f"Состояние: {self.status}\n" + format_statistics(records, VERSION) + "\n"
            "6 монет · проверка раз в 60 сек + время запросов.\n"
            "CENSORED — неоднозначные/пропущенные данные, не победа и не проигрыш.\n"
            "Реальные ордера и торговые уведомления не отправляются. "
            "Издержки и funding модельные; результаты не калибруют основной бот."
        )
