from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from cryptopilot.config import Settings
from cryptopilot.exchange import ExchangeClient, INTERVAL_MS
from cryptopilot.models import Candle


@dataclass(frozen=True, slots=True)
class LabMetrics:
    name: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    expectancy_r: float
    profit_factor: float
    max_drawdown_r: float
    total_r: float


@dataclass(frozen=True, slots=True)
class LabReport:
    symbol: str
    interval: str
    bars: int
    period_days: float
    generated_at: datetime
    metrics: tuple[LabMetrics, ...]
    hummingbot_api: str


class HummingbotLab:
    """Research-only bridge around Scaner's market data and Hummingbot V2 concepts.

    This class never places orders. If HUMMINGBOT_API_URL is configured it only
    performs a read-only health check. Live deployment remains intentionally
    separate from Scaner's advisory engine.
    """

    def __init__(self, exchange: ExchangeClient, settings: Settings) -> None:
        self.exchange = exchange
        self.settings = settings
        self._lock = asyncio.Lock()

    async def status(self) -> str:
        if not self.settings.hummingbot_api_enabled or not self.settings.hummingbot_api_url:
            return "выключен · локальная research-лаборатория работает без Hummingbot API"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.http_timeout_seconds)
            ) as session:
                async with session.get(self.settings.hummingbot_api_url.rstrip("/") + "/") as response:
                    if response.status < 400:
                        return f"API online · HTTP {response.status}"
                    return f"API отвечает с HTTP {response.status}"
        except Exception as exc:
            return f"API недоступен · {type(exc).__name__}"

    async def report(self, symbol: str = "BTCUSDT") -> LabReport:
        async with self._lock:
            candles = await self._history(symbol.upper(), self.settings.hummingbot_lab_interval)
            if len(candles) < 250:
                raise RuntimeError(
                    f"недостаточно истории для Lab: {len(candles)} свечей; нужно минимум 250"
                )
            metrics = tuple(
                self._backtest(name, candles)
                for name in ("PREMOVE", "MOMENTUM", "BREAKOUT")
            )
            start = candles[0].open_time_ms
            end = candles[-1].open_time_ms
            period_days = max(0.0, (end - start) / 86_400_000)
            return LabReport(
                symbol=symbol.upper(),
                interval=self.settings.hummingbot_lab_interval,
                bars=len(candles),
                period_days=period_days,
                generated_at=datetime.now(UTC),
                metrics=metrics,
                hummingbot_api=await self.status(),
            )

    async def _history(self, symbol: str, interval: str) -> list[Candle]:
        # Exchange clients expose a paginated history helper when available.
        historical = getattr(self.exchange, "historical_candles", None)
        if historical is not None:
            return await historical(
                symbol,
                interval,
                days=self.settings.hummingbot_lab_history_days,
            )
        return await self.exchange.candles(symbol, interval, 1000)

    def _backtest(self, name: str, candles: list[Candle]) -> LabMetrics:
        risk_values: list[float] = []
        equity = peak = 0.0
        max_dd = 0.0
        i = 55
        cooldown_until = -1
        holding_bars = max(1, self.settings.hummingbot_lab_holding_hours * 60 // _minutes(self.settings.hummingbot_lab_interval))

        while i < len(candles) - 2:
            if i < cooldown_until:
                i += 1
                continue
            side = _signal(name, candles, i)
            if side == 0:
                i += 1
                continue

            entry = candles[i + 1].open
            window = candles[max(0, i - 20): i + 1]
            atr = _atr(window)
            if atr <= 0:
                i += 1
                continue
            recent_high = max(c.high for c in window)
            recent_low = min(c.low for c in window)
            if side > 0:
                stop = min(entry - 1.15 * atr, recent_low)
                risk = entry - stop
                target = entry + 2.0 * risk
            else:
                stop = max(entry + 1.15 * atr, recent_high)
                risk = stop - entry
                target = entry - 2.0 * risk
            if risk <= 0 or risk / entry > self.settings.hummingbot_lab_max_stop_pct:
                i += 1
                continue

            result: float | None = None
            end = min(len(candles), i + 1 + holding_bars)
            for j in range(i + 1, end):
                bar = candles[j]
                stop_hit = bar.low <= stop if side > 0 else bar.high >= stop
                target_hit = bar.high >= target if side > 0 else bar.low <= target
                # Conservative OHLC assumption: when both are touched, stop wins.
                if stop_hit:
                    result = -1.0 - self.settings.hummingbot_lab_cost_r
                    break
                if target_hit:
                    result = 2.0 - self.settings.hummingbot_lab_cost_r
                    break
            if result is None:
                last = candles[end - 1].close
                result = (
                    (last - entry) / risk if side > 0 else (entry - last) / risk
                ) - self.settings.hummingbot_lab_cost_r

            result = max(-3.0, min(3.0, result))
            risk_values.append(result)
            equity += result
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            cooldown_until = end
            i = max(i + 1, end)
        wins = sum(v > 0 for v in risk_values)
        losses = len(risk_values) - wins
        gross_profit = sum(v for v in risk_values if v > 0)
        gross_loss = abs(sum(v for v in risk_values if v < 0))
        return LabMetrics(
            name=name,
            trades=len(risk_values),
            wins=wins,
            losses=losses,
            win_rate=(wins / len(risk_values) * 100) if risk_values else 0.0,
            expectancy_r=(sum(risk_values) / len(risk_values)) if risk_values else 0.0,
            profit_factor=(gross_profit / gross_loss) if gross_loss else (math.inf if gross_profit else 0.0),
            max_drawdown_r=max_dd,
            total_r=sum(risk_values),
        )

    async def compare(self, symbol: str = "BTCUSDT") -> str:
        report = await self.report(symbol)
        lines = [
            f"<b>🧪 Hummingbot Lab · {report.symbol}</b>",
            f"История: {report.period_days:.0f} дней · {report.bars} свечей · {report.interval}",
            f"Hummingbot API: {report.hummingbot_api}",
            "",
            "<b>Сравнение research-моделей (R-модель, не реальные деньги)</b>",
        ]
        for item in report.metrics:
            pf = "∞" if math.isinf(item.profit_factor) else f"{item.profit_factor:.2f}"
            lines.append(
                f"• <b>{item.name}</b>: n={item.trades}, win={item.win_rate:.1f}%, "
                f"Exp={item.expectancy_r:+.2f}R, PF={pf}, DD={item.max_drawdown_r:.2f}R, "
                f"total={item.total_r:+.2f}R"
            )
        lines.extend(
            [
                "",
                "PREMOVE — сжатие/подготовка до пробоя.",
                "MOMENTUM — EMA/RSI направленное движение.",
                "BREAKOUT — уже подтверждённый выход из диапазона.",
                "",
                "Эти тесты нужны для отбора гипотез; они не меняют PRIME/RID и не открывают сделки.",
            ]
        )
        return "\n".join(lines)


def _minutes(interval: str) -> int:
    mapping = {"1": 1, "3": 3, "5": 5, "15": 15, "30": 30, "60": 60, "120": 120, "240": 240, "D": 1440}
    if interval not in mapping:
        raise ValueError(f"Unsupported Lab interval: {interval}")
    return mapping[interval]


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    recent = candles[-period - 1:]
    trs = []
    for prev, cur in zip(recent, recent[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs) if trs else 0.0


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2 / (period + 1)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1 - alpha) * value
    return value


def _signal(name: str, candles: list[Candle], i: int) -> int:
    closes = [c.close for c in candles[: i + 1]]
    vols = [c.volume for c in candles[: i + 1]]
    recent = candles[max(0, i - 20): i]
    if len(recent) < 20:
        return 0
    close = candles[i].close
    ema20 = _ema(closes[-40:], 20)
    ema50 = _ema(closes[-70:], 50)
    avg_vol = sum(vols[-20:]) / 20
    atr = _atr(recent)
    range_high = max(c.high for c in recent)
    range_low = min(c.low for c in recent)
    range_width = range_high - range_low
    if name == "PREMOVE":
        compressed = range_width / close < 0.045 and (avg_vol > 0 and vols[-1] / avg_vol < 1.35)
        near_upper = close >= range_low + 0.65 * range_width
        near_lower = close <= range_low + 0.35 * range_width
        if compressed and ema20 > ema50 and near_upper and atr / close < 0.018:
            return 1
        if compressed and ema20 < ema50 and near_lower and atr / close < 0.018:
            return -1
        return 0
    if name == "MOMENTUM":
        rsi = _rsi(closes[-30:], 14)
        if ema20 > ema50 and rsi >= 55:
            return 1
        if ema20 < ema50 and rsi <= 45:
            return -1
        return 0
    # BREAKOUT
    if close > range_high and vols[-1] > avg_vol * 1.5:
        return 1
    if close < range_low and vols[-1] > avg_vol * 1.5:
        return -1
    return 0


def _rsi(values: list[float], period: int) -> float:
    if len(values) <= period:
        return 50.0
    gains = []
    losses = []
    for a, b in zip(values[-period - 1:], values[-period:]):
        change = b - a
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100 - (100 / (1 + avg_gain / avg_loss))
