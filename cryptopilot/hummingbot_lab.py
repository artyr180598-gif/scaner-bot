from __future__ import annotations

import asyncio
import math
from base64 import b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
import time
import aiohttp

from cryptopilot.config import Settings
from cryptopilot.exchange import ExchangeClient
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
    hummingbot_backtest: dict | None = None


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
                headers = {}
                if self.settings.hummingbot_api_username:
                    token = b64encode(
                        f"{self.settings.hummingbot_api_username}:{self.settings.hummingbot_api_password}".encode()
                    ).decode()
                    headers["Authorization"] = f"Basic {token}"
                async with session.get(
                    self.settings.hummingbot_api_url.rstrip("/") + "/", headers=headers
                ) as response:
                    if response.status < 400:
                        return f"API online · HTTP {response.status}"
                    return f"API отвечает с HTTP {response.status}"
        except Exception as exc:
            return f"API недоступен · {type(exc).__name__}"

    async def report(self, symbol: str = "BTCUSDT") -> LabReport:
        if not self.settings.hummingbot_lab_enabled:
            raise RuntimeError("HUMMINGBOT_LAB_ENABLED=false")
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
            api_backtest = await self._api_backtest(symbol.upper(), candles)
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
                hummingbot_backtest=api_backtest,
            )

    async def _api_backtest(self, symbol: str, candles: list[Candle]) -> dict | None:
        """Run the real Hummingbot V2 PREMOVE controller through the API."""
        if not self.settings.hummingbot_api_enabled or not self.settings.hummingbot_api_url:
            return None
        start = int(candles[0].open_time_ms / 1000)
        end = int((candles[-1].open_time_ms + 1000) / 1000)
        interval = self.settings.hummingbot_lab_interval
        if interval.isdigit():
            interval = f"{interval}m"
        pair = symbol.replace("/", "-").replace("USDT", "-USDT").replace("--", "-")
        if not pair.endswith("-USDT"):
            pair = f"{pair}-USDT"
        payload = {
            "start_time": start,
            "end_time": end,
            "backtesting_resolution": interval,
            "trade_cost": self.settings.hummingbot_lab_cost_r / 100.0,
            "config": {
                "id": "scaner_premove_lab",
                "controller_name": "scaner_premove_v1",
                "controller_type": "directional_trading",
                "connector_name": "binance_perpetual",
                "trading_pair": pair,
                "candles_connector": "binance_perpetual",
                "candles_trading_pair": pair,
                "interval": interval,
                "total_amount_quote": 100,
                "max_executors_per_side": 1,
                "cooldown_time": 3600,
                "leverage": 2,
                "position_mode": "HEDGE",
                "stop_loss": 0.025,
                "take_profit": 0.05,
                "time_limit": self.settings.hummingbot_lab_holding_hours * 3600,
                "take_profit_order_type": "MARKET",
                "trailing_stop": None,
                "range_length": 20,
                "ema_fast": 20,
                "ema_slow": 50,
                "max_range_pct": 0.045,
                "max_volume_ratio": 1.35,
                "long_range_position": 0.65,
                "short_range_position": 0.35,
                "max_atr_pct": 0.018,
            },
        }
        token = b64encode(
            f"{self.settings.hummingbot_api_username}:{self.settings.hummingbot_api_password}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {token}"}
        timeout = aiohttp.ClientTimeout(total=max(120, self.settings.http_timeout_seconds))
        started = time.monotonic()
        try:
            controller_payload = {"content": "\"\"\"Hummingbot V2 controller for Scaner's PRE-MOVE research hypothesis.\n\nThis controller is deliberately candle-only. It is a research baseline for Hummingbot\nbacktesting and does not attempt to reproduce Scaner's live Spot/OI/order-book PRIME stack.\n\"\"\"\n\nfrom typing import List\n\nimport pandas as pd\nfrom pydantic import Field\n\nfrom hummingbot.data_feed.candles_feed.data_types import CandlesConfig\nfrom hummingbot.strategy_v2.controllers.directional_trading_controller_base import (\n    DirectionalTradingControllerBase as _DirectionalTradingControllerBase,\n    DirectionalTradingControllerConfigBase as _DirectionalTradingControllerConfigBase,\n)\n\n\nclass AScanerPremoveV1ControllerConfig(_DirectionalTradingControllerConfigBase):\n    controller_name: str = \"scaner_premove_v1\"\n\n    candles_connector: str | None = Field(default=None)\n    candles_trading_pair: str | None = Field(default=None)\n    interval: str = Field(default=\"15m\")\n    range_length: int = Field(default=20, ge=10, le=100)\n    ema_fast: int = Field(default=20, ge=5, le=100)\n    ema_slow: int = Field(default=50, ge=10, le=200)\n    max_range_pct: float = Field(default=0.045, gt=0.005, le=0.20)\n    max_volume_ratio: float = Field(default=1.35, ge=0.5, le=3.0)\n    long_range_position: float = Field(default=0.65, ge=0.5, le=0.95)\n    short_range_position: float = Field(default=0.35, ge=0.05, le=0.5)\n    max_atr_pct: float = Field(default=0.018, gt=0.002, le=0.10)\n\n    @property\n    def resolved_candles_connector(self) -> str:\n        return self.candles_connector or self.connector_name\n\n    @property\n    def resolved_candles_pair(self) -> str:\n        return self.candles_trading_pair or self.trading_pair\n\n\nclass ScanerPremoveV1Controller(_DirectionalTradingControllerBase):\n    \"\"\"Pre-breakout compression + trend alignment controller.\"\"\"\n\n    def __init__(self, config: AScanerPremoveV1ControllerConfig, *args, **kwargs):\n        self.config = config\n        self.max_records = max(100, config.range_length + config.ema_slow + 30)\n        super().__init__(config, *args, **kwargs)\n\n    async def update_processed_data(self):\n        df = self.market_data_provider.get_candles_df(\n            connector_name=self.config.resolved_candles_connector,\n            trading_pair=self.config.resolved_candles_pair,\n            interval=self.config.interval,\n            max_records=self.max_records,\n        )\n        if df.empty or len(df) < self.max_records // 2:\n            self.processed_data[\"signal\"] = 0\n            self.processed_data[\"features\"] = df\n            return\n\n        close = df[\"close\"].astype(float)\n        volume = df[\"volume\"].astype(float)\n        ema_fast = close.ewm(span=self.config.ema_fast, adjust=False).mean()\n        ema_slow = close.ewm(span=self.config.ema_slow, adjust=False).mean()\n\n        prev_close = close.shift(1)\n        true_range = pd.concat(\n            [\n                df[\"high\"] - df[\"low\"],\n                (df[\"high\"] - prev_close).abs(),\n                (df[\"low\"] - prev_close).abs(),\n            ],\n            axis=1,\n        ).max(axis=1)\n        atr = true_range.rolling(14).mean()\n\n        range_high = df[\"high\"].rolling(self.config.range_length).max().shift(1)\n        range_low = df[\"low\"].rolling(self.config.range_length).min().shift(1)\n        range_width = (range_high - range_low) / close\n        range_position = (close - range_low) / (range_high - range_low).replace(0, pd.NA)\n        volume_ratio = volume / volume.rolling(self.config.range_length).mean()\n        atr_pct = atr / close\n\n        compressed = (\n            (range_width <= self.config.max_range_pct)\n            & (volume_ratio <= self.config.max_volume_ratio)\n            & (atr_pct <= self.config.max_atr_pct)\n        )\n        long_condition = (\n            compressed\n            & (ema_fast > ema_slow)\n            & (range_position >= self.config.long_range_position)\n        )\n        short_condition = (\n            compressed\n            & (ema_fast < ema_slow)\n            & (range_position <= self.config.short_range_position)\n        )\n\n        df[\"scaner_range_width_pct\"] = range_width * 100\n        df[\"scaner_range_position\"] = range_position\n        df[\"scaner_volume_ratio\"] = volume_ratio\n        df[\"scaner_atr_pct\"] = atr_pct * 100\n        df[\"scaner_ema_fast\"] = ema_fast\n        df[\"scaner_ema_slow\"] = ema_slow\n        df[\"signal\"] = 0\n        df.loc[long_condition, \"signal\"] = 1\n        df.loc[short_condition, \"signal\"] = -1\n\n        self.processed_data[\"signal\"] = int(df[\"signal\"].iloc[-1])\n        self.processed_data[\"features\"] = df\n\n    def get_candles_config(self) -> List[CandlesConfig]:\n        return [\n            CandlesConfig(\n                connector=self.config.resolved_candles_connector,\n                trading_pair=self.config.resolved_candles_pair,\n                interval=self.config.interval,\n                max_records=self.max_records,\n            )\n        ]\n", "type": "directional_trading"}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.settings.hummingbot_api_url.rstrip("/") + "/controllers/directional_trading/scaner_premove_v1",
                    json=controller_payload,
                    headers=headers,
                ) as controller_response:
                    if controller_response.status >= 400:
                        body = await controller_response.json(content_type=None)
                        return {"error": body.get("detail", body), "http": controller_response.status, "stage": "controller_upload"}
                parsed_config = payload["config"]
                async with session.post(
                    self.settings.hummingbot_api_url.rstrip("/") + "/controllers/configs/scaner_premove_v1_btc",
                    json=parsed_config,
                    headers=headers,
                ) as config_response:
                    if config_response.status >= 400:
                        body = await config_response.json(content_type=None)
                        return {"error": body.get("detail", body), "http": config_response.status, "stage": "config_upload"}
                async with session.post(
                    self.settings.hummingbot_api_url.rstrip("/") + "/backtesting/run",
                    json=payload,
                    headers=headers,
                ) as response:
                    body = await response.json(content_type=None)
                    if response.status >= 400:
                        return {"error": body.get("detail", body), "http": response.status}
                    result = body.get("results", body if isinstance(body, dict) else {})
                    return {"results": result, "elapsed_seconds": round(time.monotonic() - started, 2), "pair": pair, "resolution": interval}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        # Keep the Lab independent from Telegram/CCXT/Hummingbot pair notation.
        # Bybit market-data requests must receive BTCUSDT, while Hummingbot
        # configs use BTC-USDT. This also handles BTC-USDT:USDT safely.
        normalized = (symbol or "").strip().upper().split(":", 1)[0]
        normalized = normalized.replace("/", "").replace("-", "").replace("_", "")
        if normalized.endswith("USDTUSDT"):
            normalized = normalized[:-4]
        if not normalized:
            raise ValueError("Hummingbot Lab: empty trading symbol")
        return normalized

    async def _history(self, symbol: str, interval: str) -> list[Candle]:
        # Normalize once at the Lab boundary so every exchange implementation
        # receives the same canonical market symbol.
        market_symbol = self._normalize_symbol(symbol)
        log = __import__("logging").getLogger(__name__)
        log.info(
            "Hummingbot Lab history request: exchange=%s symbol=%s interval=%s days=%s",
            self.exchange.name, market_symbol, interval,
            self.settings.hummingbot_lab_history_days,
        )
        historical = getattr(self.exchange, "historical_candles", None)
        try:
            if historical is not None:
                return await historical(
                    market_symbol,
                    interval,
                    days=self.settings.hummingbot_lab_history_days,
                )
            return await self.exchange.candles(market_symbol, interval, 1000)
        except Exception as exc:
            raise RuntimeError(
                f"Hummingbot Lab history failed: exchange={self.exchange.name} "
                f"symbol={market_symbol} interval={interval} · {type(exc).__name__}: {exc}"
            ) from exc

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
        ]
        if report.hummingbot_backtest:
            hb = report.hummingbot_backtest
            if "error" in hb:
                lines.append(f"Hummingbot V2 PREMOVE: ошибка · {hb['error']}")
            else:
                r = hb.get("results", {})
                lines.append(
                    "<b>Hummingbot V2 PREMOVE (реальный engine):</b> "
                    f"PNL={r.get('net_pnl_quote', r.get('net_pnl', 0))}, "
                    f"DD={r.get('max_drawdown', 0)}, Sharpe={r.get('sharpe_ratio', 0)}, "
                    f"PF={r.get('profit_factor', 0)}, "
                    f"executors={r.get('total_executors_with_position', r.get('total_executors', 0))}, "
                    f"time={hb.get('elapsed_seconds', 0)}s"
                )
        lines.extend([
            "<b>Сравнение research-моделей (R-модель, не реальные деньги)</b>",
        ])
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
