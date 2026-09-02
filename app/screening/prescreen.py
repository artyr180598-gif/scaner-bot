"""
app/screening/prescreen.py — двухступенчатый отбор монет.

Продуктовая идея: глубокий анализ (4 таймфрейма, структура, профиль объёма)
стоит запросов и времени, поэтому его нельзя запускать по 300 монетам.
Отбор идёт в две ступени:

  1. ``coarse_screen`` — только по тикерам (один запрос на биржу на всю
     вселенную): ликвидность, «не улетела ли», сжатие 24-часового диапазона.
  2. ``fine_screen`` — по свечам базового таймфрейма: реальное сжатие
     волатильности, 7-дневная динамика, аномалии объёма, позиция в диапазоне.

Главный принцип обеих ступеней — НЕ брать уже сильно выросшие монеты:
нам нужен потенциал движения, а не догонялки.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from app.domain.models import Candles, MarketSnapshot, PrescreenCandidate, TickerInfo
from app.indicators.volatility import analyse_volatility

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PrescreenConfig:
    min_quote_volume: float = 3_000_000
    max_change_z: float = 2.2          # кросс-секционный z изменения за 24 ч
    max_change_24h_pct: float = 18.0   # жёсткий потолок «уже улетела»
    min_range_24h_pct: float = 1.0     # слишком мёртвая монета
    max_range_24h_pct: float = 30.0    # слишком бешеная (новостной памп)
    min_change_7d_abs: float = 45.0    # уже сделала движение за неделю
    max_distance_high_pct: float = 2.0  # стоит у самого хая без отката
    coarse_candidates: int = 90
    fine_candidates: int = 35
    volume_anomaly_z: float = 1.8


# ---------------------------------------------------------------------------
# Ступень 1: по тикерам
# ---------------------------------------------------------------------------

def coarse_screen(
    tickers: Dict[str, TickerInfo],
    stats: Optional[Dict[str, float]] = None,
    cfg: Optional[PrescreenConfig] = None,
) -> List[PrescreenCandidate]:
    """Дешёвый отбор по 24-часовой статистике. Возвращает отсортированный список."""
    cfg = cfg or PrescreenConfig()
    stats = stats or {}
    median = stats.get("change_24h_median", 0.0)
    std = stats.get("change_24h_std", 0.0) or 1.0

    out: List[PrescreenCandidate] = []
    for symbol, t in tickers.items():
        price = t.last
        if not price or price != price or price <= 0:
            continue
        if t.quote_volume < cfg.min_quote_volume:
            continue

        change = t.change_pct if t.change_pct == t.change_pct else 0.0
        z = (change - median) / std if std else 0.0

        # Жёсткие отсечения «уже улетела».
        if change > cfg.max_change_24h_pct:
            continue
        if z > cfg.max_change_z:
            continue

        rng_pct = ((t.high - t.low) / price * 100
                   if t.high == t.high and t.low == t.low and t.high > t.low else float("nan"))
        if rng_pct != rng_pct:
            continue
        if rng_pct < cfg.min_range_24h_pct or rng_pct > cfg.max_range_24h_pct:
            continue

        position = (price - t.low) / (t.high - t.low) if t.high > t.low else 0.5

        score = 0.0
        reasons: List[str] = []

        # 1) Сжатие диапазона: узкий диапазон = копится энергия.
        compression = _bell(rng_pct, center=4.0, width=8.0)
        score += 0.35 * compression
        if compression > 0.6:
            reasons.append(f"узкий диапазон 24ч ({rng_pct:.1f}%)")

        # 2) Не улетела: чем ближе к медиане рынка — тем лучше.
        not_ran = _bell(abs(z), center=0.0, width=1.6)
        score += 0.30 * not_ran
        if not_ran > 0.6:
            reasons.append(f"движение за 24ч {change:+.1f}% — близко к рынку")

        # 3) Ликвидность: предпочитаем средний сегмент (миллиардники двигаются вяло).
        liq = _liquidity_score(t.quote_volume)
        score += 0.20 * liq

        # 4) Позиция в диапазоне: середина/низ лучше, чем «прижат к хаю».
        pos_score = 1.0 - abs(position - 0.45)
        score += 0.15 * max(0.0, pos_score)
        if position > 0.92:
            score -= 0.1
            reasons.append("цена прижата к максимуму дня")

        out.append(PrescreenCandidate(
            symbol=symbol, base=symbol.split("/")[0], score=float(score), ticker=t,
            reasons=reasons[:3],
            metrics={"range_24h_pct": rng_pct, "change_24h": change, "z": z,
                     "quote_volume": t.quote_volume, "position": position},
        ))

    out.sort(key=lambda c: -c.score)
    return out[: cfg.coarse_candidates]


# ---------------------------------------------------------------------------
# Ступень 2: по свечам
# ---------------------------------------------------------------------------

def fine_screen(
    snapshots: Sequence[MarketSnapshot],
    cfg: Optional[PrescreenConfig] = None,
    signal_tf=None,
) -> List[PrescreenCandidate]:
    """
    Отбор по реальным свечам: сжатие волатильности, недельная динамика,
    аномалия объёма. Вызывается уже после загрузки базового таймфрейма.
    """
    cfg = cfg or PrescreenConfig()
    out: List[PrescreenCandidate] = []
    for snap in snapshots:
        candles = _primary_candles(snap, signal_tf)
        if candles is None or len(candles) < 80:
            continue
        state = analyse_volatility(candles.high, candles.low, candles.close,
                                   percentile_window=min(120, len(candles)))
        if state is None:
            continue

        price = candles.last_price
        week = candles.close.iloc[-min(len(candles), 168):]
        change_7d = (price / float(week.iloc[0]) - 1) * 100 if len(week) else 0.0
        window = candles.close.iloc[-min(len(candles), 720):]
        hi, lo = float(window.max()), float(window.min())
        from_high = (price / hi - 1) * 100 if hi else 0.0

        # Анти-погоня: монета уже сделала движение — пропускаем.
        if abs(change_7d) > cfg.min_change_7d_abs:
            continue
        if from_high > -cfg.max_distance_high_pct and change_7d > 10:
            continue

        volume_z = _last_zscore(candles.volume)
        score = 0.0
        reasons: List[str] = []

        score += 0.40 * state.compression
        if state.compression >= 0.55:
            reasons.append(
                f"волатильность на {state.atr_percentile:.0f} перцентиле — сжатие")
        if state.squeeze_active:
            score += 0.05
            reasons.append("BB сжат внутри KC")

        # Неделю стояла — значит движение впереди.
        flat = _bell(abs(change_7d), center=0.0, width=18.0)
        score += 0.25 * flat
        if flat > 0.7:
            reasons.append(f"за 7 дней {change_7d:+.1f}% — движение ещё не сделано")

        # Аномалия объёма без движения — набор позиции.
        if volume_z == volume_z and volume_z >= cfg.volume_anomaly_z and abs(change_7d) < 12:
            score += 0.15
            reasons.append(f"объём z={volume_z:.1f} при плоской цене")

        # Близость к границе диапазона — триггер пробоя.
        pos = (price - lo) / (hi - lo) if hi > lo else 0.5
        edge = 1.0 - min(1.0, min(pos, 1 - pos) / 0.25)
        score += 0.10 * edge
        if edge > 0.6:
            reasons.append("цена у границы 30-дневного диапазона")

        if snap.ticker is not None:
            score += 0.10 * _liquidity_score(snap.ticker.quote_volume)

        out.append(PrescreenCandidate(
            symbol=snap.symbol, base=snap.base, score=float(min(1.0, score)),
            ticker=snap.ticker or TickerInfo(symbol=snap.symbol, last=price),
            reasons=reasons[:4],
            metrics={
                "compression": state.compression,
                "atr_percentile": state.atr_percentile,
                "change_7d": change_7d,
                "distance_from_high_pct": from_high,
                "volume_z": volume_z,
                "range_position": pos,
                "quote_volume": snap.ticker.quote_volume if snap.ticker else 0.0,
            },
            exchange=snap.exchange,
        ))

    out.sort(key=lambda c: -c.score)
    return out[: cfg.fine_candidates]


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _bell(value: float, center: float, width: float) -> float:
    """1 в центре, 0 на расстоянии width — «колокол» без тяжёлой математики."""
    if value != value or width <= 0:
        return 0.0
    return float(max(0.0, 1.0 - abs(value - center) / width))


def _liquidity_score(quote_volume: float) -> float:
    """
    Пик интереса — 20–300M$ оборота: достаточно ликвидны, но ещё двигаются.
    Миллиардники (BTC/ETH) двигаются вяло, тонкие монеты неисполнимы.
    """
    if quote_volume <= 0:
        return 0.0
    logv = math.log10(quote_volume)
    return float(max(0.0, 1.0 - abs(logv - 7.9) / 1.6))


def _last_zscore(volume, period: int = 20) -> float:
    if len(volume) < period + 1:
        return float("nan")
    window = volume.iloc[-(period + 1):-1]
    mean = float(window.mean())
    std = float(window.std(ddof=0))
    if std == 0 or mean == 0:
        return float("nan")
    return float((float(volume.iloc[-1]) - mean) / std)


def _primary_candles(snap: MarketSnapshot, signal_tf=None) -> Optional[Candles]:
    if signal_tf is not None and signal_tf in snap.candles:
        return snap.candles[signal_tf]
    if not snap.candles:
        return None
    tf = min(snap.candles, key=lambda t: t.minutes)
    return snap.candles[tf]
