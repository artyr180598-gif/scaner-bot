"""
app/data/synthetic.py — генератор синтетического рынка.

Зачем: сеть песочницы/CI часто не пускает к биржам, а проверять ядро надо.
Генератор строит РЕАЛИСТИЧНЫЕ ряды (свечи с тенями, объёмом, режимами
волатильности), на которых можно гонять весь конвейер: скрининг → анализ →
сигнал → рендер. Это не замена бектесту на реальных данных (см. backtest/),
но это честная проверка логики и регрессий.

Режимы:
  accumulation — флэт со сжатием волатильности и растущим объёмом («пружина»);
  pumped       — монета уже улетела (сканер обязан её НЕ рекомендовать);
  downtrend    — устойчивое снижение (ожидаем SHORT);
  range        — боковик без тренда (ожидаем WAIT);
  breakout     — сжатие, затем импульс вверх;
  capitulation — резкий пролив с отбоем (свип ликвидности).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from app.domain.models import (Candles, DerivativesInfo, MarketSnapshot, Timeframe,
                               TickerInfo)

REGIMES = ("accumulation", "pumped", "downtrend", "range", "breakout", "capitulation")


@dataclass(slots=True)
class RegimeSpec:
    drift: float = 0.0            # средний лог-доходностный дрейф за бар
    vol: float = 0.010            # базовая волатильность за бар
    vol_trend: float = 0.0        # как меняется волатильность к концу ряда
    volume_trend: float = 0.0     # тренд объёма (доля за весь ряд)
    tail_drift: float = 0.0       # дрейф последних 10% баров
    tail_bars: float = 0.10


def _spec(regime: str) -> RegimeSpec:
    return {
        "accumulation": RegimeSpec(drift=0.0001, vol=0.012, vol_trend=-0.55,
                                   volume_trend=0.6, tail_drift=0.0008),
        "pumped": RegimeSpec(drift=0.004, vol=0.018, vol_trend=0.25, volume_trend=0.8,
                             tail_drift=0.006),
        "downtrend": RegimeSpec(drift=-0.0022, vol=0.014, vol_trend=0.1,
                                volume_trend=0.3, tail_drift=-0.003),
        "range": RegimeSpec(drift=0.0, vol=0.008, vol_trend=0.0, volume_trend=0.0),
        "breakout": RegimeSpec(drift=0.0002, vol=0.011, vol_trend=-0.6, volume_trend=0.4,
                               tail_drift=0.004),
        "capitulation": RegimeSpec(drift=-0.001, vol=0.013, vol_trend=0.4,
                                   volume_trend=0.5, tail_drift=-0.02),
    }[regime]


def make_candles(
    symbol: str = "TEST/USDT",
    timeframe: Timeframe | str = Timeframe.H1,
    bars: int = 500,
    regime: str = "accumulation",
    seed: int = 42,
    start_price: float = 1.0,
    end_time: Optional[datetime] = None,
    add_wicks: bool = True,
    inject_sweep: bool = True,
) -> Candles:
    """Строит синтетические свечи в выбранном режиме."""
    if regime not in REGIMES:
        raise ValueError(f"неизвестный режим {regime!r}; доступны: {REGIMES}")
    tf = timeframe if isinstance(timeframe, Timeframe) else Timeframe.parse(timeframe)
    spec = _spec(regime)
    rng = np.random.default_rng(seed)

    n = int(bars)
    tail_start = int(n * (1 - spec.tail_bars))
    vols = np.linspace(spec.vol, max(0.002, spec.vol * (1 + spec.vol_trend)), n)
    drifts = np.full(n, spec.drift)
    drifts[tail_start:] = spec.tail_drift

    # Режимные «волны» — чтобы структура (свинги) была похожа на реальную.
    # Амплитуда намеренно мала: в кумулятивном выражении волна даёт ±5–8%,
    # иначе она перекрывает дрейф режима и все режимы выглядят одинаково.
    cycle = np.sin(np.arange(n) / (n / 12.0)) * spec.vol * 0.12
    shocks = rng.normal(0, 1, n) * vols + drifts + cycle * 0.4
    log_close = np.cumsum(shocks)
    close = start_price * np.exp(log_close)

    if regime == "capitulation" and inject_sweep:
        # Резкий пролив с возвратом — классический свип ликвидности.
        dip = int(n * 0.93)
        close[dip:] = close[dip:] * np.linspace(0.90, 0.985, n - dip)
    if regime == "breakout" and inject_sweep:
        impulse = int(n * 0.9)
        close[impulse:] = close[impulse:] * np.linspace(1.0, 1.09, n - impulse)

    open_ = np.concatenate([[start_price], close[:-1]])
    body = np.abs(close - open_)
    if add_wicks:
        upper = rng.uniform(0.1, 0.9, n) * (body + vols * close * 0.8)
        lower = rng.uniform(0.1, 0.9, n) * (body + vols * close * 0.8)
    else:
        upper = np.zeros(n)
        lower = np.zeros(n)
    high = np.maximum(open_, close) + upper
    low = np.minimum(open_, close) - lower
    low = np.maximum(low, close * 0.5)

    base_volume = 1_000_000.0
    vol_series = base_volume * (1 + np.linspace(0, spec.volume_trend, n))
    vol_series *= rng.uniform(0.6, 1.5, n)
    # Объём растёт на сильных барах.
    vol_series *= 1 + 3 * np.abs(shocks) / max(spec.vol, 1e-9)
    if regime == "accumulation":
        # Накопление: объём подрастает в конце при плоской цене.
        vol_series[tail_start:] *= np.linspace(1.0, 1.8, n - tail_start)

    end = end_time or datetime.now(timezone.utc).replace(second=0, microsecond=0)
    step = timedelta(minutes=tf.minutes)
    ts = [end - step * (n - 1 - i) for i in range(n)]
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(ts, utc=True),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": vol_series,
        }
    )
    df.index = df["timestamp"]
    return Candles(symbol=symbol, timeframe=tf, df=df, last_closed=True,
                   source="synthetic")


def make_snapshot(
    symbol: str = "TEST/USDT",
    regime: str = "accumulation",
    seed: int = 42,
    base_tf: Timeframe | str = Timeframe.H1,
    bars: int = 500,
    daily_bars: int = 300,
    quote_volume: float = 25_000_000.0,
    funding_rate: Optional[float] = 0.0001,
    oi_change_pct: Optional[float] = 6.0,
    exchange: str = "synthetic",
    universe_stats: Optional[Dict[str, float]] = None,
) -> MarketSnapshot:
    """
    Полный снимок: базовый ТФ + ресемплинг в 4h + отдельные дневные свечи,
    тикер и деривативы. По структуре — как то, что отдаёт MarketDataService.
    """
    base_tf = (base_tf if isinstance(base_tf, Timeframe)
               else Timeframe.parse(base_tf))
    base = make_candles(symbol, base_tf, bars, regime, seed=seed)
    daily = make_candles(symbol, Timeframe.D1, daily_bars, regime, seed=seed + 1,
                         start_price=float(base.close.iloc[0]))
    # Синхронизируем дневки по последней цене базового ТФ (иначе контекст врёт).
    scale = float(base.close.iloc[-1]) / float(daily.close.iloc[-1])
    daily.df[["open", "high", "low", "close"]] *= scale
    daily = Candles(symbol, Timeframe.D1, daily.df, last_closed=True, source="synthetic")

    candles: Dict[Timeframe, Candles] = {base_tf: base, Timeframe.D1: daily}
    for tf in (Timeframe.M15, Timeframe.H4):
        if tf.minutes > base_tf.minutes:
            try:
                candles[tf] = base.resample(tf)
            except ValueError:
                continue

    price = float(base.close.iloc[-1])
    day = base.close.iloc[-min(len(base), 24):]
    ticker = TickerInfo(
        symbol=symbol, last=price,
        quote_volume=quote_volume, base_volume=quote_volume / max(price, 1e-9),
        change_pct=float((price / float(day.iloc[0]) - 1) * 100),
        high=float(base.high.iloc[-24:].max()), low=float(base.low.iloc[-24:].min()),
        bid=price * 0.9995, ask=price * 1.0005,
    )
    derivatives = DerivativesInfo(
        funding_rate=funding_rate, open_interest=1_000_000.0,
        open_interest_value=1_000_000.0 * price,
        open_interest_change_pct=oi_change_pct,
        mark_price=price * 1.0002, index_price=price, available=funding_rate is not None,
    )
    return MarketSnapshot(
        symbol=symbol, base=symbol.split("/")[0], quote=symbol.split("/")[1],
        exchange=exchange, candles=candles, ticker=ticker, derivatives=derivatives,
        universe_stats=universe_stats or {"change_24h_median": 0.0, "change_24h_std": 2.5},
    )


def make_universe(
    symbols: Sequence[str],
    regimes: Sequence[str],
    seed: int = 1,
    **kwargs,
) -> List[MarketSnapshot]:
    """Набор снимков для тестов скрининга (по одному режиму на монету)."""
    out: List[MarketSnapshot] = []
    for i, (symbol, regime) in enumerate(zip(symbols, regimes)):
        out.append(make_snapshot(symbol, regime, seed=seed + i * 7, **kwargs))
    return out
