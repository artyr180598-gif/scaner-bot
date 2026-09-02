"""
app/indicators/structure.py — структура рынка и смарт-мани (SMC) на чистых числах.

Что здесь считается:
  * свинги (фрактальные экстремумы с подтверждением ``right`` барами — без
    заглядывания в будущее: свинг становится известен только когда закрылось
    ``right`` баров после него);
  * события BOS (Break Of Structure — продолжение тренда) и
    CHOCH (Change of Character — смена характера рынка);
  * Order Blocks — последний противоположный ордер перед импульсным
    смещением (displacement), которое и сломало структуру;
  * FVG (Fair Value Gaps / имбалансы) и их заполненность;
  * ликвидации/снятие ликвидности (sweep): тень за свинг с возвратом внутрь.

Интерпретация (баллы, факторы) живёт в ``app/analysis/`` — здесь только факты.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

KIND_HIGH = "high"
KIND_LOW = "low"

EVENT_BOS = "BOS"
EVENT_CHOCH = "CHOCH"

DIR_UP = "up"
DIR_DOWN = "down"


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Swing:
    kind: str            # KIND_HIGH | KIND_LOW
    index: int
    price: float

    @property
    def is_high(self) -> bool:
        return self.kind == KIND_HIGH


@dataclass(slots=True)
class StructureEvent:
    kind: str            # EVENT_BOS | EVENT_CHOCH
    direction: str       # DIR_UP | DIR_DOWN
    index: int           # бар, на котором закрытие пробило уровень
    level: float         # пробитый уровень (цена свинга)
    bars_ago: int = 0


@dataclass(slots=True)
class Zone:
    """Order Block или FVG — ценовая зона интереса."""

    kind: str            # "bull_ob" | "bear_ob" | "bull_fvg" | "bear_fvg"
    low: float
    high: float
    index: int
    strength: float = 0.0     # 0..1 (импульс/объём)
    mitigated: bool = False   # цена уже заходила внутрь
    touches: int = 0          # сколько раз тестировали
    bars_ago: int = 0

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    @property
    def is_bullish(self) -> bool:
        return self.kind in ("bull_ob", "bull_fvg")

    def distance_pct(self, price: float) -> float:
        """Расстояние от цены до зоны в % (0 — внутри зоны)."""
        if self.low <= price <= self.high:
            return 0.0
        ref = self.high if price > self.high else self.low
        return (price / ref - 1) * 100


@dataclass(slots=True)
class LiquiditySweep:
    direction: str       # DIR_UP — сняли ликвидность сверху (медвежий разворот)
    index: int
    level: float         # пробитый уровень ликвидности
    wick: float          # экстремум тени
    close_inside: bool = True
    bars_ago: int = 0


@dataclass(slots=True)
class StructureState:
    """Сводка структуры для скоринга."""

    trend: str = "range"                 # up | down | range
    last_event: Optional[StructureEvent] = None
    events: List[StructureEvent] = field(default_factory=list)
    swings: List[Swing] = field(default_factory=list)
    last_high: Optional[float] = None    # последний подтверждённый свинг-хай
    last_low: Optional[float] = None
    equal_highs: List[float] = field(default_factory=list)   # пулы ликвидности
    equal_lows: List[float] = field(default_factory=list)
    sweeps: List[LiquiditySweep] = field(default_factory=list)
    order_blocks: List[Zone] = field(default_factory=list)
    fvgs: List[Zone] = field(default_factory=list)

    @property
    def hh_hl(self) -> bool:
        highs = [s.price for s in self.swings if s.is_high]
        lows = [s.price for s in self.swings if not s.is_high]
        return len(highs) >= 2 and len(lows) >= 2 and highs[-1] > highs[-2] and lows[-1] > lows[-2]

    @property
    def lh_ll(self) -> bool:
        highs = [s.price for s in self.swings if s.is_high]
        lows = [s.price for s in self.swings if not s.is_high]
        return len(highs) >= 2 and len(lows) >= 2 and highs[-1] < highs[-2] and lows[-1] < lows[-2]


# ---------------------------------------------------------------------------
# Свинги
# ---------------------------------------------------------------------------

def find_swings(high: pd.Series, low: pd.Series, left: int = 3,
                right: int = 3, confirm: bool = True) -> List[Swing]:
    """
    Фрактальные свинги.

    ``confirm=True`` (по умолчанию) — возвращает только те свинги, которые уже
    подтверждены ``right`` барами справа. Последний такой свинг находится не
    дальше ``right`` баров от конца ряда: это и есть защита от lookahead.
    """
    h = high.to_numpy(dtype="float64")
    lo = low.to_numpy(dtype="float64")
    n = len(h)
    left, right = int(left), int(right)
    if n < left + right + 2:
        return []

    limit = n - right if confirm else n
    out: List[Swing] = []
    for i in range(left, limit):
        window_h = h[i - left:i + right + 1]
        window_l = lo[i - left:i + right + 1]
        if np.isnan(window_h).any() or np.isnan(window_l).any():
            continue
        # Экстремум в окне. Если вершин несколько (плоская вершина), берём
        # ПЕРВУЮ — иначе на «полочке» свинг не находится вовсе.
        max_h = window_h.max()
        min_l = window_l.min()
        if h[i] == max_h and not np.any(window_h[:left] == max_h):
            out.append(Swing(KIND_HIGH, i, float(h[i])))
        if lo[i] == min_l and not np.any(window_l[:left] == min_l):
            out.append(Swing(KIND_LOW, i, float(lo[i])))
    out.sort(key=lambda s: s.index)
    return out


def equal_levels(swings: List[Swing], tolerance_pct: float = 0.35) -> Tuple[List[float], List[float]]:
    """
    «Равные хаи/лои» — магниты ликвидности (стопы за двойной вершиной).
    Возвращает (equal_highs, equal_lows).
    """
    highs = sorted(s.price for s in swings if s.is_high)
    lows = sorted(s.price for s in swings if not s.is_high)

    def _cluster(values: List[float]) -> List[float]:
        clusters: List[List[float]] = []
        for v in values:
            if clusters and abs(v - clusters[-1][-1]) / clusters[-1][-1] * 100 <= tolerance_pct:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [sum(c) / len(c) for c in clusters if len(c) >= 2]

    return _cluster(highs), _cluster(lows)


# ---------------------------------------------------------------------------
# BOS / CHOCH
# ---------------------------------------------------------------------------

def detect_structure(close: pd.Series, swings: List[Swing]) -> StructureState:
    """
    Проход по барам: фиксируем пробои подтверждённых свингов.

    Логика SMC:
      * если рынок делал HH/HL (тренд вверх) и закрытие пробило последний
        свинг-хай — это BOS вверх (продолжение);
      * если рынок делал LH/LL (тренд вниз) и закрытие пробило последний
        свинг-хай — это CHOCH вверх (первый признак смены тренда).
    """
    closes = close.to_numpy(dtype="float64")
    n = len(closes)
    state = StructureState(swings=list(swings))
    trend = "range"
    last_high: Optional[Swing] = None
    last_low: Optional[Swing] = None
    events: List[StructureEvent] = []

    # Индекс «свинг становится видимым» = index + right (мы его уже подтвердили).
    by_bar: dict[int, List[Swing]] = {}
    for s in swings:
        by_bar.setdefault(s.index, []).append(s)

    pending_high: Optional[Swing] = None
    pending_low: Optional[Swing] = None

    for i in range(n):
        for s in by_bar.get(i, []):
            if s.is_high:
                pending_high = s
            else:
                pending_low = s

        if np.isnan(closes[i]):
            continue

        # Пробой вверх.
        if pending_high is not None and pending_high.index < i and closes[i] > pending_high.price:
            kind = EVENT_BOS if trend == DIR_UP else EVENT_CHOCH
            events.append(StructureEvent(kind, DIR_UP, i, pending_high.price, n - 1 - i))
            trend = DIR_UP
            pending_high = None       # уровень пробит — ждём новый свинг-хай
        # Пробой вниз.
        if pending_low is not None and pending_low.index < i and closes[i] < pending_low.price:
            kind = EVENT_BOS if trend == DIR_DOWN else EVENT_CHOCH
            events.append(StructureEvent(kind, DIR_DOWN, i, pending_low.price, n - 1 - i))
            trend = DIR_DOWN
            pending_low = None

        # Обновляем «последние» уровни для плана сделки.
        if pending_high is not None:
            last_high = pending_high
        if pending_low is not None:
            last_low = pending_low

    state.trend = trend
    state.events = events
    state.last_event = events[-1] if events else None
    state.last_high = last_high.price if last_high else None
    state.last_low = last_low.price if last_low else None
    state.equal_highs, state.equal_lows = equal_levels(swings)
    return state


# ---------------------------------------------------------------------------
# Order Blocks / FVG / Sweeps
# ---------------------------------------------------------------------------

def detect_order_blocks(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    open_: pd.Series,
    volume: pd.Series,
    events: List[StructureEvent],
    lookback_bars: int = 6,
    min_displacement_atr: float = 1.2,
    atr_value: float = float("nan"),
    max_zones: int = 6,
) -> List[Zone]:
    """
    Order Block = последняя противоположная свеча перед импульсом,
    который сломал структуру. Зона = тело+тени этой свечи.
    """
    n = len(close)
    if n < 10:
        return []
    h = high.to_numpy(dtype="float64")
    lo = low.to_numpy(dtype="float64")
    c = close.to_numpy(dtype="float64")
    o = open_.to_numpy(dtype="float64")
    v = volume.to_numpy(dtype="float64")
    v_mean = float(np.nanmean(v[-50:])) if np.isfinite(v[-50:]).any() else 0.0

    if not np.isfinite(atr_value) or atr_value <= 0:
        # Грубая оценка ATR, если его не передали.
        tr = np.maximum(h[1:] - lo[1:], np.abs(np.diff(c)))
        atr_value = float(np.nanmean(tr[-14:])) if len(tr) else float("nan")

    zones: List[Zone] = []
    for ev in sorted(events, key=lambda e: -e.index)[: max_zones * 2]:
        i = ev.index
        if i < lookback_bars + 1 or i >= n:
            continue
        # Импульс: движение от минимума/максимума окна до пробоя.
        window = slice(i - lookback_bars, i + 1)
        if ev.direction == DIR_UP:
            move = c[i] - lo[window].min()
            threshold = min_displacement_atr * atr_value
            if move < threshold:
                continue
            # Ищем последнюю медвежью свечу перед импульсом.
            for j in range(i - 1, max(0, i - lookback_bars) - 1, -1):
                if c[j] < o[j]:
                    strength = _zone_strength(move, atr_value, v[j], v_mean)
                    zones.append(Zone("bull_ob", float(lo[j]), float(h[j]), j,
                                      strength, bars_ago=n - 1 - j))
                    break
        else:
            move = hi_max(h[window]) - c[i]
            threshold = min_displacement_atr * atr_value
            if move < threshold:
                continue
            for j in range(i - 1, max(0, i - lookback_bars) - 1, -1):
                if c[j] > o[j]:
                    strength = _zone_strength(move, atr_value, v[j], v_mean)
                    zones.append(Zone("bear_ob", float(lo[j]), float(h[j]), j,
                                      strength, bars_ago=n - 1 - j))
                    break

    # Митигация: цена заходила в зону после её формирования.
    last_price = float(c[-1])
    for z in zones:
        after = slice(z.index + 1, n)
        if z.is_bullish:
            z.touches = int(np.sum(lo[after] <= z.high))
            z.mitigated = bool(np.sum(lo[after] <= z.mid))
        else:
            z.touches = int(np.sum(h[after] >= z.low))
            z.mitigated = bool(np.sum(h[after] >= z.mid))
        z.bars_ago = n - 1 - z.index

    # Дедуп по пересечению и сортировка по близости к текущей цене.
    zones.sort(key=lambda z: (z.mitigated, abs(z.distance_pct(last_price))))
    return zones[:max_zones]


def hi_max(arr: np.ndarray) -> float:
    return float(np.nanmax(arr)) if len(arr) else float("nan")


def _zone_strength(move: float, atr_value: float, vol: float, vol_mean: float) -> float:
    """0..1: чем сильнее импульс и больше объём на блоке — тем значимее зона."""
    if not np.isfinite(atr_value) or atr_value <= 0:
        return 0.5
    impulse = min(1.0, (move / atr_value) / 4.0)
    vol_part = 0.5
    if vol_mean > 0 and np.isfinite(vol):
        vol_part = min(1.0, (vol / vol_mean) / 2.0)
    return float(max(0.0, min(1.0, 0.7 * impulse + 0.3 * vol_part)))


def detect_fvgs(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    max_zones: int = 6,
    max_age_bars: int = 120,
) -> List[Zone]:
    """
    Fair Value Gap: трёхсвечной имбаланс.
      бычий  — low[i] > high[i-2]  → зона (high[i-2], low[i]);
      медвежий — high[i] < low[i-2] → зона (high[i], low[i-2]).
    """
    n = len(close)
    if n < 5:
        return []
    h = high.to_numpy(dtype="float64")
    lo = low.to_numpy(dtype="float64")
    c = close.to_numpy(dtype="float64")
    last_price = float(c[-1])
    zones: List[Zone] = []
    start = max(2, n - max_age_bars)
    for i in range(start, n):
        if lo[i] > h[i - 2]:
            zones.append(Zone("bull_fvg", float(h[i - 2]), float(lo[i]), i,
                              strength=min(1.0, (lo[i] - h[i - 2]) / max(last_price, 1e-9) * 100),
                              bars_ago=n - 1 - i))
        elif h[i] < lo[i - 2]:
            zones.append(Zone("bear_fvg", float(h[i]), float(lo[i - 2]), i,
                              strength=min(1.0, (lo[i - 2] - h[i]) / max(last_price, 1e-9) * 100),
                              bars_ago=n - 1 - i))

    # Заполненность: сколько цены «прошли» сквозь зону после формирования.
    for z in zones:
        after = slice(z.index + 1, n)
        if z.is_bullish:
            z.touches = int(np.sum(lo[after] <= z.high))
            z.mitigated = bool(np.sum(lo[after] <= z.mid)) if n > z.index + 1 else False
        else:
            z.touches = int(np.sum(h[after] >= z.low))
            z.mitigated = bool(np.sum(h[after] >= z.mid)) if n > z.index + 1 else False

    zones.sort(key=lambda z: (z.mitigated, abs(z.distance_pct(last_price))))
    return zones[:max_zones]


def detect_sweeps(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    swings: List[Swing],
    max_age_bars: int = 30,
    max_events: int = 4,
) -> List[LiquiditySweep]:
    """
    Снятие ликвидности: тень прошила уровень свинга, а закрытие вернулось внутрь.
    Сверху — медвежий признак, снизу — бычий (stop hunt + разворот).
    """
    n = len(close)
    if n < 5:
        return []
    h = high.to_numpy(dtype="float64")
    lo = low.to_numpy(dtype="float64")
    c = close.to_numpy(dtype="float64")
    out: List[LiquiditySweep] = []
    start = max(1, n - max_age_bars)
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]

    for i in range(start, n):
        for s in highs:
            if s.index >= i - 1:
                continue
            if h[i] > s.price and c[i] < s.price:
                out.append(LiquiditySweep(DIR_UP, i, s.price, float(h[i]), True, n - 1 - i))
        for s in lows:
            if s.index >= i - 1:
                continue
            if lo[i] < s.price and c[i] > s.price:
                out.append(LiquiditySweep(DIR_DOWN, i, s.price, float(lo[i]), True, n - 1 - i))

    out.sort(key=lambda e: -e.index)
    return out[:max_events]


def analyse_structure(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    open_: pd.Series,
    volume: pd.Series,
    left: int = 3,
    right: int = 3,
    atr_value: float = float("nan"),
) -> StructureState:
    """Единая точка входа: свинги → события → OB/FVG/sweeps."""
    swings = find_swings(high, low, left, right, confirm=True)
    state = detect_structure(close, swings)
    state.order_blocks = detect_order_blocks(
        high, low, close, open_, volume, state.events, atr_value=atr_value)
    state.fvgs = detect_fvgs(high, low, close)
    state.sweeps = detect_sweeps(high, low, close, swings)
    return state
