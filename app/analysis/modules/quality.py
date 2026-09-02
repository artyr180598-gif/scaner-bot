"""
app/analysis/modules/quality.py — качество данных и исполнимости.

Эти факторы НЕ влияют на направление: они снижают уверенность, когда сигнал
нельзя ни показать, ни исполнить (мало истории, тонкий стакан, протухшие
данные, широкий спред). Отдельная «уверенность в данных» — принцип, который
проект выстрадал на практике: нельзя смешивать «я уверен в идее» и «я уверен,
что данные верные».

Факторы группы QUALITY имеют score ≤ 0 (0 = проблем нет).
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp
from app.domain.models import Factor


@analysis_module("data_freshness", Group.QUALITY,
                 description="Свежесть котировок")
def data_freshness(features: MarketFeatures) -> Iterable[Factor]:
    age = features.snapshot.staleness_seconds()
    tf_seconds = features.signal_tf.minutes * 60
    if age <= tf_seconds:
        return []
    lag = age / tf_seconds
    score = -clamp((lag - 1) / 4)
    if score > -0.05:
        return []
    yield Factor("data_freshness", Group.QUALITY, score,
                 f"последняя свеча {age / 60:.0f} мин назад (устарела на {lag:.1f} бара)",
                 weight=1.0)


@analysis_module("liquidity_quality", Group.QUALITY,
                 description="Ликвидность: оборот и спред")
def liquidity_quality(features: MarketFeatures) -> Iterable[Factor]:
    ticker = features.ticker
    if ticker is None:
        return []
    score = 0.0
    notes: list[str] = []
    if ticker.quote_volume < 1_000_000:
        score -= 0.5
        notes.append(f"оборот {ticker.quote_volume / 1e6:.2f}M$ — тонкий рынок")
    elif ticker.quote_volume < 5_000_000:
        score -= 0.2
        notes.append(f"оборот {ticker.quote_volume / 1e6:.1f}M$ — умеренный")
    spread = ticker.spread_pct
    if spread == spread and spread > 0.2:
        penalty = min(0.5, (spread - 0.2) / 1.0)
        score -= penalty
        notes.append(f"спред {spread:.2f}% съест часть движения")
    if score >= -0.05:
        return []
    yield Factor("liquidity_quality", Group.QUALITY, clamp(score, -1.0, 0.0),
                 "; ".join(notes), weight=1.2)


@analysis_module("history_depth", Group.QUALITY,
                 description="Достаточно ли истории для честного анализа")
def history_depth(features: MarketFeatures) -> Iterable[Factor]:
    bars = features.primary().bars
    if bars >= 300:
        return []
    if bars >= 200:
        score, note = -0.1, f"история {bars} баров — EMA200 недоступна"
    elif bars >= 120:
        score, note = -0.3, f"история {bars} баров — часть индикаторов неточна"
    else:
        score, note = -0.6, f"всего {bars} баров истории — анализ ненадёжен"
    yield Factor("history_depth", Group.QUALITY, score, note, weight=1.0)


@analysis_module("signal_space", Group.QUALITY,
                 description="Хватает ли места под стоп и цели (не входить в стену)")
def signal_space(features: MarketFeatures) -> Iterable[Factor]:
    atr = features.atr
    price = features.price
    if not atr or atr != atr or not features.levels:
        return []
    ups = [lv.price for lv in features.levels if lv.price > price]
    downs = [lv.price for lv in features.levels if lv.price < price]
    space_up = (min(ups) - price) / atr if ups else 99.0
    space_dn = (price - max(downs)) / atr if downs else 99.0
    if min(space_up, space_dn) < 0.8 and max(space_up, space_dn) < 2.5:
        yield Factor("signal_space", Group.QUALITY, -0.20,
                     f"мало места: {space_dn:.1f} ATR вниз и {space_up:.1f} ATR вверх",
                     weight=0.7)
