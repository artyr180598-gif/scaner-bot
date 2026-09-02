"""
app/analysis/modules/momentum.py — импульс: RSI, MACD, Stochastic, дивергенции.

Осознанно не используем «RSI < 30 → покупай»: на трендовом рынке это ловля
падающих ножей. Вместо этого оцениваем СОСТОЯНИЕ и РАЗВОРОТ импульса
(пересечения, смена направления гистограммы, дивергенции).
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp, detect_divergence, finite, scaled
from app.domain.models import Factor


@analysis_module("rsi_state", Group.MOMENTUM,
                 description="Состояние и разворот RSI (не просто зона)")
def rsi_state(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    rsi = finite(ind.rsi, float("nan"))
    if rsi != rsi:
        return []
    series: pd.Series | None = ind.rsi_series  # type: ignore[assignment]
    slope = float("nan")
    if series is not None and len(series) >= 4:
        recent = series.iloc[-4:].dropna()
        if len(recent) >= 3:
            slope = float(recent.iloc[-1] - recent.iloc[0])

    score = 0.0
    notes: list[str] = [f"RSI {rsi:.0f}"]
    if rsi < 30 and slope > 1:
        score = 0.85
        notes.append("перепродан и разворачивается вверх")
    elif rsi < 35:
        score = 0.35
        notes.append("перепродан")
    elif rsi > 70 and slope < -1:
        score = -0.85
        notes.append("перекуплен и разворачивается вниз")
    elif rsi > 65:
        score = -0.35
        notes.append("перекуплен")
    else:
        # Центральная зона: важен наклон и сторона от 50.
        score = clamp(0.45 * scaled(rsi, 40, 60) + 0.55 * scaled(slope, -6, 6))
        notes.append("наклон вверх" if slope > 1 else
                     ("наклон вниз" if slope < -1 else "без явного наклона"))
    yield Factor("rsi_state", Group.MOMENTUM, score, ", ".join(notes), weight=1.0)


@analysis_module("macd_momentum", Group.MOMENTUM,
                 description="Гистограмма MACD: знак и ускорение")
def macd_momentum(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    hist = finite(ind.macd_hist, float("nan"))
    prev = finite(ind.macd_hist_prev, float("nan"))
    if hist != hist:
        return []
    sign = 1.0 if hist > 0 else (-1.0 if hist < 0 else 0.0)
    accel = 0.0
    note = "гистограмма MACD у нуля"
    if hist == hist and prev == prev:
        growing = abs(hist) > abs(prev)
        if growing:
            accel = 0.35 * sign
            note = (f"MACD усиливается {'вверх' if sign > 0 else 'вниз'} "
                    f"(гистограмма {hist:+.4g} против {prev:+.4g})")
        else:
            accel = -0.25 * sign
            note = (f"MACD затухает ({'бычий' if sign > 0 else 'медвежий'} импульс "
                    f"выдыхается: {hist:+.4g} против {prev:+.4g})")
    crossed = (hist > 0 > prev) or (hist < 0 < prev)
    if crossed:
        note = f"пересечение сигнальной линии MACD ({'бычье' if hist > 0 else 'медвежье'})"
    score = clamp(0.6 * sign + accel + (0.3 if crossed else 0.0) * sign)
    yield Factor("macd_momentum", Group.MOMENTUM, score, note, weight=1.0)


@analysis_module("stoch_state", Group.MOMENTUM,
                 description="Stochastic: пересечения в зонах перекупленности")
def stoch_state(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    k, d = finite(ind.stoch_k, float("nan")), finite(ind.stoch_d, float("nan"))
    if k != k or d != d:
        return []
    score = 0.0
    if k > d and k < 30:
        score = 0.7
        note = f"Stoch {k:.0f}/{d:.0f} — бычье пересечение в перепроданности"
    elif k < d and k > 70:
        score = -0.7
        note = f"Stoch {k:.0f}/{d:.0f} — медвежье пересечение в перекупленности"
    elif k < 20:
        score = 0.25
        note = f"Stoch {k:.0f} — зона перепроданности"
    elif k > 80:
        score = -0.25
        note = f"Stoch {k:.0f} — зона перекупленности"
    else:
        score = 0.3 * (1.0 if k > d else -1.0)
        note = f"Stoch {k:.0f}/{d:.0f} — {'бычий' if k > d else 'медвежий'} наклон"
    yield Factor("stoch_state", Group.MOMENTUM, score, note, weight=0.6)


@analysis_module("momentum_divergence", Group.MOMENTUM,
                 description="Классические дивергенции RSI и MACD")
def momentum_divergence(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    struct = features.struct(ind.timeframe)
    if struct is None or ind.rsi_series is None or ind.close_series is None:
        return []
    found: list[tuple[float, str]] = []

    rsi_type, rsi_text = detect_divergence(
        ind.close_series, ind.rsi_series, struct.swings, kind="classic")  # type: ignore[arg-type]
    if rsi_type:
        found.append((0.9 if rsi_type == "bullish" else -0.9, f"RSI: {rsi_text}"))

    hist_type, hist_text = detect_divergence(
        ind.close_series, ind.macd_hist_series, struct.swings, kind="classic")  # type: ignore[arg-type]
    if hist_type:
        found.append((0.6 if hist_type == "bullish" else -0.6, f"MACD: {hist_text}"))

    if not found:
        return []
    score = clamp(sum(f[0] for f in found) / len(found))
    yield Factor("momentum_divergence", Group.MOMENTUM, score,
                 " | ".join(f[1] for f in found), weight=1.4)


@analysis_module("hidden_divergence", Group.MOMENTUM,
                 description="Скрытые дивергенции — признак продолжения тренда")
def hidden_divergence(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    struct = features.struct(ind.timeframe)
    if struct is None or ind.rsi_series is None or ind.close_series is None:
        return []
    kind, text = detect_divergence(
        ind.close_series, ind.rsi_series, struct.swings, kind="hidden")  # type: ignore[arg-type]
    if not kind:
        return []
    yield Factor("hidden_divergence", Group.MOMENTUM,
                 0.65 if kind == "bullish" else -0.65, text, weight=0.7)


@analysis_module("oscillator_extremes", Group.MOMENTUM,
                 description="Экстремумы CCI/Williams %R как триггер разворота")
def oscillator_extremes(features: MarketFeatures) -> Iterable[Factor]:
    ind = features.primary()
    cci, wr = finite(ind.cci, float("nan")), finite(ind.williams_r, float("nan"))
    votes: list[float] = []
    notes: list[str] = []
    if cci == cci:
        if cci < -120:
            votes.append(0.7)
            notes.append(f"CCI {cci:.0f} (сильная перепроданность)")
        elif cci > 120:
            votes.append(-0.7)
            notes.append(f"CCI {cci:.0f} (сильная перекупленность)")
    if wr == wr:
        if wr < -85:
            votes.append(0.5)
            notes.append(f"Williams %R {wr:.0f}")
        elif wr > -15:
            votes.append(-0.5)
            notes.append(f"Williams %R {wr:.0f}")
    if not votes:
        return []
    yield Factor("oscillator_extremes", Group.MOMENTUM,
                 clamp(sum(votes) / len(votes)), ", ".join(notes), weight=0.5)
