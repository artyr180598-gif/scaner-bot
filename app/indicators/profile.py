"""
app/indicators/profile.py — профиль объёма (Volume Profile).

Считаем «по ценовым уровням», а не по времени: разбиваем диапазон N баров на
бины и суммируем объём каждого (аппроксимация равномерным распределением объёма
бара между low и high — стандартный подход, когда нет тиковых данных).

Результат: POC (самый торгуемый уровень), VAH/VAL (границы 70% объёма),
HVN/LVN (зоны принятия/отторжения) — всё это магниты и опоры для целей.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass(slots=True)
class VolumeNode:
    price_low: float
    price_high: float
    volume: float

    @property
    def mid(self) -> float:
        return (self.price_low + self.price_high) / 2


@dataclass(slots=True)
class VolumeProfile:
    poc: float
    vah: float
    val: float
    nodes: List[VolumeNode] = field(default_factory=list)
    total_volume: float = 0.0
    bins: int = 0
    low: float = float("nan")
    high: float = float("nan")

    def hvn(self, top: int = 3) -> List[VolumeNode]:
        """Зоны принятия — максимумы объёма (цели/магниты)."""
        return sorted(self.nodes, key=lambda n: -n.volume)[:top]

    def lvn(self, top: int = 3) -> List[VolumeNode]:
        """Зоны отторжения — минимумы объёма (цена проходит их быстро)."""
        if not self.nodes:
            return []
        return sorted(self.nodes, key=lambda n: n.volume)[:top]

    def nearest_level(self, price: float, kind: str = "all") -> Optional[VolumeNode]:
        """Ближайший значимый уровень к цене (для целей/входа)."""
        if kind == "hvn":
            pool = self.hvn(4)
        elif kind == "lvn":
            pool = self.lvn(4)
        else:
            pool = self.hvn(4)
        pool = [n for n in pool if n.mid != price]
        if not pool:
            return None
        return min(pool, key=lambda n: abs(n.mid - price))

    def describe_ru(self) -> str:
        return f"POC {self.poc:.6g} · VA {self.val:.6g}–{self.vah:.6g}"


def build_volume_profile(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    lookback: int = 200,
    bins: int = 48,
    value_area: float = 0.70,
) -> Optional[VolumeProfile]:
    """Строит профиль объёма за последние ``lookback`` баров."""
    n = min(len(close), int(lookback))
    if n < 20:
        return None
    h = high.iloc[-n:].to_numpy(dtype="float64")
    lo = low.iloc[-n:].to_numpy(dtype="float64")
    c = close.iloc[-n:].to_numpy(dtype="float64")
    v = volume.iloc[-n:].to_numpy(dtype="float64")

    mask = np.isfinite(h) & np.isfinite(lo) & np.isfinite(c) & np.isfinite(v) & (v > 0) & (h > lo)
    if mask.sum() < 10:
        return None
    h, lo, c, v = h[mask], lo[mask], c[mask], v[mask]

    price_low = float(lo.min())
    price_high = float(h.max())
    if price_high <= price_low:
        return None

    edges = np.linspace(price_low, price_high, int(bins) + 1)
    counts = np.zeros(int(bins), dtype="float64")

    # Объём бара распределяем по бинам, которые пересекает диапазон [low, high].
    for i in range(len(c)):
        start = np.searchsorted(edges, lo[i], side="right") - 1
        end = np.searchsorted(edges, h[i], side="right") - 1
        start = max(0, min(int(bins) - 1, start))
        end = max(0, min(int(bins) - 1, end))
        span = end - start + 1
        # Типичная цена получает больший вес — приближение «где торговали».
        weights = np.ones(span, dtype="float64")
        tp_bin = np.searchsorted(edges, c[i], side="right") - 1
        tp_bin = max(0, min(int(bins) - 1, tp_bin))
        if start <= tp_bin <= end:
            weights[tp_bin - start] += 1.0
        counts[start:end + 1] += v[i] * weights / weights.sum()

    nodes = [
        VolumeNode(float(edges[i]), float(edges[i + 1]), float(counts[i]))
        for i in range(int(bins))
    ]
    total = float(counts.sum())
    if total <= 0:
        return None
    poc_idx = int(np.argmax(counts))
    poc = nodes[poc_idx].mid

    # Value Area: расширяемся от POC, пока не наберём value_area объёма.
    target = total * value_area
    acc = counts[poc_idx]
    left, right = poc_idx - 1, poc_idx + 1
    while acc < target and (left >= 0 or right < len(counts)):
        lv = counts[left] if left >= 0 else -1.0
        rv = counts[right] if right < len(counts) else -1.0
        if lv >= rv:
            acc += lv
            left -= 1
        else:
            acc += rv
            right += 1
    val = nodes[max(0, left + 1)].price_low
    vah = nodes[min(len(nodes) - 1, right - 1)].price_high

    return VolumeProfile(
        poc=poc, vah=float(vah), val=float(val), nodes=nodes,
        total_volume=total, bins=int(bins), low=price_low, high=price_high,
    )
