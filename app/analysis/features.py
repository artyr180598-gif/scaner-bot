"""
app/analysis/features.py — сбор признаков (аналог ``populate_indicators``).

Один проход по снимку рынка → ``MarketFeatures``: индикаторы по каждому ТФ,
состояние волатильности, структура, профиль объёма, список уровней, производные
метрики (сжатие, «растянутость», аномалия объёма, funding/OI).

Всё считается один раз и переиспользуется модулями анализа — иначе каждый
модуль заново гонял бы EMA/ADX по 700 барам.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from app.analysis.base import Level, MarketFeatures, TimeframeIndicators
from app.analysis.utils import clamp
from app.domain.models import (Candles, MarketContext, MarketSnapshot,
                               Timeframe)
from app.indicators import core as ind
from app.indicators.profile import build_volume_profile
from app.indicators.structure import analyse_structure
from app.indicators.volatility import analyse_volatility, choppiness_index

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Индикаторы одного таймфрейма
# ---------------------------------------------------------------------------

def compute_timeframe_indicators(candles: Candles) -> TimeframeIndicators:
    """Считает полный набор индикаторов для одного ТФ (только на прошлых барах)."""
    df = candles.df
    close, high, low, open_, volume = (
        candles.close, candles.high, candles.low, candles.open, candles.volume)

    out = TimeframeIndicators(timeframe=candles.timeframe, candles=candles,
                              bars=len(candles))
    if len(candles) < 5:
        return out

    macd_line, macd_sig, macd_hist = ind.macd(close)
    k, d = ind.stochastic(high, low, close)
    bb_mid, bb_up, bb_low, _ = ind.bollinger(close, 20, 2.0)
    adx_v, pdi, mdi = ind.adx(high, low, close, 14)
    _, st_dir = ind.supertrend(high, low, close, 10, 3.0)
    vol_sma = ind.sma(volume, 20)
    obv_s = ind.obv(close, volume)
    obv_slope = (obv_s / obv_s.shift(20).replace(0, np.nan) - 1) * 100

    def _last(series: pd.Series) -> float:
        return ind.last_valid(series)

    out.rsi = _last(ind.rsi(close, 14))
    out.macd = _last(macd_line)
    out.macd_signal = _last(macd_sig)
    out.macd_hist = _last(macd_hist)
    out.macd_hist_prev = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 and \
        not pd.isna(macd_hist.iloc[-2]) else float("nan")
    out.ema20 = _last(ind.ema(close, 20))
    out.ema50 = _last(ind.ema(close, 50))
    out.ema100 = _last(ind.ema(close, 100))
    out.ema200 = _last(ind.ema(close, 200))
    out.adx = _last(adx_v)
    out.plus_di = _last(pdi)
    out.minus_di = _last(mdi)
    out.stoch_k = _last(k)
    out.stoch_d = _last(d)
    out.bb_mid = _last(bb_mid)
    out.bb_upper = _last(bb_up)
    out.bb_lower = _last(bb_low)
    out.bb_percent_b = ((out.price - out.bb_lower) / (out.bb_upper - out.bb_lower)
                        if out.bb_upper > out.bb_lower else float("nan"))
    out.atr = _last(ind.atr(high, low, close, 14))
    out.atr_pct = _last(ind.natr(high, low, close, 14))
    out.mfi = _last(ind.mfi(high, low, close, volume, 14))
    out.cci = _last(ind.cci(high, low, close, 20))
    out.williams_r = _last(ind.williams_r(high, low, close, 14))
    out.obv_slope_pct = _last(obv_slope)
    out.volume_ratio = (float(volume.iloc[-1] / vol_sma.iloc[-1])
                        if len(vol_sma) and vol_sma.iloc[-1] and not pd.isna(vol_sma.iloc[-1])
                        else float("nan"))
    out.volume_z = _last(ind.zscore(volume, 20))
    out.roc_short = _last(ind.roc(close, 10))
    out.roc_long = _last(ind.roc(close, 30))
    out.slope_pct = _last(ind.linear_slope(close, 20, normalize=True))
    out.trend_r2 = _last(ind.r_squared(close, 20))
    out.supertrend_dir = _last(st_dir)
    out.vwap = _last(ind.vwap(high, low, close, volume,
                              period=_vwap_period(candles.timeframe)))
    out.choppiness = _last(choppiness_index(high, low, close, 14))
    out.rsi_series = ind.rsi(close, 14)
    out.close_series = close
    out.macd_hist_series = macd_hist
    return out


def _vwap_period(tf: Timeframe) -> int:
    """Скользящий VWAP за «сутки» в барах текущего ТФ."""
    return max(10, int(1440 / tf.minutes))


# ---------------------------------------------------------------------------
# Оценка тренда одного таймфрейма (используется и модулем, и метриками)
# ---------------------------------------------------------------------------

TF_WEIGHT = {
    Timeframe.M5: 0.4,
    Timeframe.M15: 0.5,
    Timeframe.M30: 0.6,
    Timeframe.H1: 0.8,
    Timeframe.H4: 1.0,
    Timeframe.D1: 1.2,
}


def timeframe_trend_score(ind: TimeframeIndicators) -> tuple[float, str]:
    """
    Оценка тренда одного таймфрейма в [-1, 1] + пояснение для интерфейса.

    Четыре независимых взгляда: расположение EMA, наклон регрессии,
    Supertrend и позиция относительно EMA200/EMA100.
    """
    votes: list[float] = []
    notes: list[str] = []

    stack = ind.ema_stack()
    if stack == "up":
        votes.append(1.0)
        notes.append("EMA выстроены вверх")
    elif stack == "down":
        votes.append(-1.0)
        notes.append("EMA выстроены вниз")

    if ind.slope_pct == ind.slope_pct:
        # Наклон в %/бар: ±0.35% и больше считаем сильным трендом.
        votes.append(clamp(ind.slope_pct / 0.35))

    if ind.supertrend_dir == ind.supertrend_dir and ind.supertrend_dir != 0:
        votes.append(1.0 if ind.supertrend_dir > 0 else -1.0)

    if ind.ema200 == ind.ema200 and ind.price:
        ratio = ind.price / ind.ema200 - 1
        votes.append(clamp(ratio / 0.08))
        notes.append("цена выше EMA200" if ratio > 0 else "цена ниже EMA200")
    elif ind.ema100 == ind.ema100 and ind.price:
        votes.append(clamp((ind.price / ind.ema100 - 1) / 0.08))

    if not votes:
        return 0.0, ""
    return clamp(sum(votes) / len(votes)), "; ".join(notes[:2])


def higher_tf_trend(features: "MarketFeatures") -> float:
    """Взвешенный тренд старших таймфреймов в [-1, 1] (0 — нет данных)."""
    num = 0.0
    den = 0.0
    for tf in features.higher_tfs():
        ind = features.ind(tf)
        if ind is None or not ind.usable:
            continue
        score, _ = timeframe_trend_score(ind)
        weight = TF_WEIGHT.get(tf, 0.7)
        num += score * weight
        den += weight
    return clamp(num / den) if den else 0.0


# ---------------------------------------------------------------------------
# Уровни
# ---------------------------------------------------------------------------

def round_levels(price: float, count: int = 2) -> List[float]:
    """
    «Круглые» уровни (0.50, 1.00, 100, 1000…). Психологические зоны,
    где реально стоят стопы и лимитки.
    """
    if not price or price <= 0 or not math.isfinite(price):
        return []
    magnitude = 10 ** math.floor(math.log10(price))
    steps = [magnitude / 2, magnitude, magnitude * 2]
    out: List[float] = []
    for step in steps:
        base = math.floor(price / step) * step
        out.extend([base, base + step])
    # Оставляем ближайшие к цене, уникальные.
    uniq = sorted(set(round(v, 12) for v in out if v > 0),
                  key=lambda v: abs(v - price))
    return uniq[:count]


def collect_levels(features: MarketFeatures, max_levels: int = 14) -> List[Level]:
    """
    Собирает значимые уровни из разных источников и убирает дубликаты
    (уровни ближе 0.3 ATR друг к другу склеиваются, сила суммируется).
    """
    price = features.price
    atr = features.atr
    levels: List[Level] = []

    # 1) Свинги старших таймфреймов — самые надёжные опоры.
    for tf, struct in features.structure.items():
        weight = 0.5 + 0.12 * (tf.minutes / 60)
        for swing in struct.swings[-14:]:
            kind = "resistance" if swing.is_high else "support"
            levels.append(Level(swing.price, kind, "swing",
                                strength=min(1.0, weight), touches=1))
        for eq in struct.equal_highs:
            levels.append(Level(eq, "resistance", "eq_high", strength=0.8))
        for eq in struct.equal_lows:
            levels.append(Level(eq, "support", "eq_low", strength=0.8))

    # 2) Order Blocks и FVG — зоны, куда цена любит возвращаться.
    for tf, struct in features.structure.items():
        for zone in struct.order_blocks[:3]:
            levels.append(Level(zone.mid, "support" if zone.is_bullish else "resistance",
                                "ob", strength=0.4 + 0.4 * zone.strength,
                                touches=zone.touches))
        for zone in struct.fvgs[:3]:
            levels.append(Level(zone.mid, "support" if zone.is_bullish else "resistance",
                                "fvg", strength=0.35, touches=zone.touches))

    # 3) Профиль объёма.
    if features.profile is not None:
        vp = features.profile
        levels.append(Level(vp.poc, "support" if price > vp.poc else "resistance",
                            "poc", strength=0.85))
        levels.append(Level(vp.vah, "resistance", "vah", strength=0.6))
        levels.append(Level(vp.val, "support", "val", strength=0.6))

    # 4) Круглые числа.
    for lvl in round_levels(price):
        levels.append(Level(lvl, "resistance" if lvl > price else "support",
                            "round", strength=0.4))

    # 5) Границы диапазона (30 баров) — цели движения.
    for tf, vol_state in features.volatility.items():
        if vol_state.donchian_high > price:
            levels.append(Level(vol_state.donchian_high, "resistance", "range_high", 0.55))
        if vol_state.donchian_low < price:
            levels.append(Level(vol_state.donchian_low, "support", "range_low", 0.55))

    return _merge_levels(levels, price, atr, max_levels)


def _merge_levels(levels: List[Level], price: float, atr: float,
                  max_levels: int) -> List[Level]:
    """Склеивает близкие уровни и оставляет самые сильные рядом с ценой."""
    tol = max(atr * 0.35, price * 0.0015) if atr and atr == atr else price * 0.002
    levels = [lv for lv in levels if lv.price > 0 and math.isfinite(lv.price)]
    levels.sort(key=lambda lv: lv.price)
    merged: List[Level] = []
    for lv in levels:
        if merged and abs(lv.price - merged[-1].price) <= tol:
            prev = merged[-1]
            prev.strength = min(1.0, prev.strength + 0.25 * lv.strength)
            prev.touches += lv.touches
            prev.source = prev.source if prev.strength >= lv.strength else lv.source
            prev.price = (prev.price + lv.price) / 2
        else:
            merged.append(Level(lv.price, lv.kind, lv.source, lv.strength, lv.touches))
    # Пересчитываем сторону относительно текущей цены.
    for lv in merged:
        lv.kind = "resistance" if lv.price > price else "support"
    merged.sort(key=lambda lv: (-min(1.0, lv.strength + lv.touches * 0.05),
                                abs(lv.price - price)))
    return merged[:max_levels]


# ---------------------------------------------------------------------------
# Производные метрики
# ---------------------------------------------------------------------------

def _bars_for_hours(tf: Timeframe, hours: int) -> int:
    return max(1, int(hours * 60 / tf.minutes))


def compute_metrics(features: MarketFeatures) -> Dict[str, float]:
    """Метрики, которые нужны и скринингу, и объяснениям в сигнале."""
    metrics: Dict[str, float] = {}
    primary = features.primary()
    tf = primary.timeframe
    candles = primary.candles
    price = features.price

    for hours, key in ((24, "change_24h"), (168, "change_7d"), (720, "change_30d")):
        n = _bars_for_hours(tf, hours)
        if len(candles) > n:
            ref = float(candles.close.iloc[-n - 1])
            metrics[key] = (price / ref - 1) * 100 if ref else float("nan")
        else:
            metrics[key] = float("nan")

    # Диапазон и положение в нём.
    lookback = min(len(candles), _bars_for_hours(tf, 720))
    window = candles.close.iloc[-lookback:]
    hi, lo = float(window.max()), float(window.min())
    metrics["range_30d_high"] = hi
    metrics["range_30d_low"] = lo
    metrics["distance_from_high_pct"] = (price / hi - 1) * 100 if hi else float("nan")
    metrics["distance_from_low_pct"] = (price / lo - 1) * 100 if lo else float("nan")
    metrics["range_position"] = (price - lo) / (hi - lo) if hi > lo else 0.5

    # «Растянутость» — сколько ATR от быстрой EMA: защита от входа в погоню.
    if primary.ema20 == primary.ema20 and primary.atr and primary.atr > 0:
        metrics["stretch_atr"] = (price - primary.ema20) / primary.atr
    else:
        metrics["stretch_atr"] = float("nan")

    metrics["volume_z"] = primary.volume_z
    metrics["volume_ratio"] = primary.volume_ratio
    metrics["obv_slope_pct"] = primary.obv_slope_pct

    # Сжатие волатильности (основной ТФ и старший).
    for state_tf, state in features.volatility.items():
        suffix = state_tf.value.replace("m", "").replace("h", "h")
        metrics[f"compression_{suffix}"] = state.compression
        metrics[f"atr_percentile_{suffix}"] = state.atr_percentile
    vol_state = features.volatility.get(features.signal_tf) or (
        next(iter(features.volatility.values()), None))
    if vol_state:
        metrics["compression"] = vol_state.compression
        metrics["atr_percentile"] = vol_state.atr_percentile
        metrics["bb_width_percentile"] = vol_state.bb_width_percentile
        metrics["choppiness"] = vol_state.choppiness

    # Деривативы.
    der = features.snapshot.derivatives
    if der.funding_rate is not None:
        metrics["funding_pct"] = der.funding_pct or float("nan")
    if der.open_interest_change_pct is not None:
        metrics["oi_change_pct"] = der.open_interest_change_pct
    if (basis := der.basis_pct) is not None:
        metrics["basis_pct"] = basis

    # Тикер.
    ticker = features.snapshot.ticker
    if ticker:
        metrics["quote_volume_usd"] = ticker.quote_volume
        metrics["spread_pct"] = ticker.spread_pct
        metrics["ticker_change_24h"] = ticker.change_pct

    # Кросс-секционный контекст (насколько монета «убежала» относительно рынка).
    stats = features.snapshot.universe_stats
    if stats.get("change_24h_std"):
        metrics["change_z"] = (
            (metrics.get("change_24h", 0.0) - stats.get("change_24h_median", 0.0))
            / stats["change_24h_std"]
        )

    # Тренд старших таймфреймов: используется модулями уровней, чтобы гасить
    # контртрендовые идеи (возврат к среднему в сильном тренде — ловля ножей).
    metrics["htf_trend"] = higher_tf_trend(features)
    metrics["signal_tf_trend"] = timeframe_trend_score(features.primary())[0]
    return metrics


def counter_trend_damping(features: MarketFeatures, direction_sign: float,
                          max_damping: float = 0.65) -> float:
    """
    Множитель 0.35..1.0 для контртрендовых факторов.

    Если старшие таймфреймы сильно трендят ПРОТИВ идеи, факторы возврата
    к среднему (уровни, диапазон, профиль объёма) гасятся: они работают во
    флэте, а в тренде превращаются в ловлю падающих ножей.
    """
    htf = features.metrics.get("htf_trend", 0.0)
    if not htf or htf != htf:
        return 1.0
    conflict = -direction_sign * htf       # >0 — идея против тренда
    if conflict <= 0:
        return 1.0
    return float(max(1.0 - max_damping, 1.0 - conflict))


def compute_flags(features: MarketFeatures) -> Dict[str, bool]:
    """Булевы признаки для фильтров (анти-погоня, ликвидность, свежесть)."""
    m = features.metrics
    flags: Dict[str, bool] = {}
    atr = features.atr

    flags["extended_up"] = m.get("stretch_atr", 0.0) == m.get("stretch_atr", 0.0) \
        and m.get("stretch_atr", 0.0) > 2.0
    flags["extended_down"] = m.get("stretch_atr", 0.0) == m.get("stretch_atr", 0.0) \
        and m.get("stretch_atr", 0.0) < -2.0
    flags["near_high"] = m.get("distance_from_high_pct", -100.0) > -3.0
    flags["near_low"] = m.get("distance_from_low_pct", 100.0) < 3.0
    flags["already_ran"] = (m.get("change_7d", 0.0) == m.get("change_7d", 0.0)
                            and m.get("change_7d", 0.0) > 25.0)
    flags["volume_spike"] = m.get("volume_z", 0.0) == m.get("volume_z", 0.0) \
        and m.get("volume_z", 0.0) > 2.0
    flags["compressed"] = m.get("compression", 1.0) >= 0.55
    flags["illiquid"] = (features.ticker is not None
                         and features.ticker.quote_volume < 500_000)
    flags["wide_spread"] = (features.ticker is not None
                            and features.ticker.spread_pct == features.ticker.spread_pct
                            and features.ticker.spread_pct > 0.35)
    # «Протухло» — относительно периода: для 1h норма — закрытие в пределах
    # полутора часов, для 15m — в пределах 22 минут.
    stale_grace = max(600.0, features.signal_tf.minutes * 60 * 1.5)
    flags["stale"] = features.snapshot.staleness_seconds() > stale_grace

    # Вход «в уровень» — цена в пределах 1 ATR от значимой поддержки/сопротивления.
    support = features.nearest_level("support", max_distance_atr=1.5)
    resistance = features.nearest_level("resistance", max_distance_atr=1.5)
    flags["at_support"] = support is not None
    flags["at_resistance"] = resistance is not None
    flags["atr_ok"] = bool(atr and atr == atr and atr > 0)
    return flags


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def build_features(
    snapshot: MarketSnapshot,
    signal_tf: Timeframe,
    context: Optional[MarketContext] = None,
    structure_tfs: Optional[List[Timeframe]] = None,
    volatility_tfs: Optional[List[Timeframe]] = None,
) -> MarketFeatures:
    """
    Собирает ``MarketFeatures`` по снимку рынка.

    ``structure_tfs``/``volatility_tfs`` позволяют не считать тяжёлые вещи
    на всех таймфреймах (структура на 5m — шум и лишние миллисекунды).
    """
    features = MarketFeatures(
        snapshot=snapshot,
        signal_tf=signal_tf,
        context=context or MarketContext(),
    )

    for tf, candles in snapshot.candles.items():
        if len(candles) < 5:
            features.notes.append(f"{tf.value}: слишком мало свечей ({len(candles)})")
            continue
        features.indicators[tf] = compute_timeframe_indicators(candles)

    if not features.indicators:
        features.notes.append("нет данных ни по одному таймфрейму")
        features.metrics = compute_metrics(features) if features.indicators else {}
        return features

    # Если сигнального ТФ в снимке нет — берём ближайший доступный.
    if signal_tf not in features.indicators:
        signal_tf = min(features.indicators, key=lambda t: abs(t.minutes - signal_tf.minutes))
        features.signal_tf = signal_tf

    vol_tfs = volatility_tfs or [signal_tf] + features.higher_tfs()
    for tf in dict.fromkeys(vol_tfs):
        candles = snapshot.candles.get(tf)
        if candles is None or len(candles) < 30:
            continue
        state = analyse_volatility(candles.high, candles.low, candles.close)
        if state:
            features.volatility[tf] = state

    struct_tfs = structure_tfs or [signal_tf] + features.higher_tfs()
    for tf in dict.fromkeys(struct_tfs):
        candles = snapshot.candles.get(tf)
        if candles is None or len(candles) < 40:
            continue
        tf_ind = features.indicators.get(tf)
        features.structure[tf] = analyse_structure(
            candles.high, candles.low, candles.close, candles.open, candles.volume,
            left=3, right=3, atr_value=tf_ind.atr if tf_ind else float("nan"),
        )

    # Профиль объёма — на самом длинном доступном ряде.
    profile_tf = max(features.indicators, key=lambda t: len(snapshot.candles[t]))
    candles = snapshot.candles[profile_tf]
    features.profile = build_volume_profile(
        candles.high, candles.low, candles.close, candles.volume,
        lookback=min(len(candles), 300), bins=60,
    )

    features.metrics = compute_metrics(features)
    features.levels = collect_levels(features)
    features.flags = compute_flags(features)
    return features
