"""
app/signals/planner.py — построение плана сделки.

Принципы (важнее, чем конкретные коэффициенты):
  * ВХОД — не «по рынку», а зона: незакрытый Order Block / FVG / сильный
    уровень в пределах пары ATR. Если цена уже ушла — сигнал не публикуется
    (анти-погоня), а не «входите по любой цене».
  * СТОП — за структурой (свинг/зона) с буфером, но не дальше разумного
    лимита в ATR. Риск = расстояние до стопа, от него считается объём позиции.
  * ЦЕЛИ — реальные магниты: пулы ликвидности, уровни, POC/VAH, имбалансы;
    если их не хватает, добавляются ATR-цели, чтобы целей всегда было три.
  * R:R считается от СРЕДНЕЙ точки входа (честно для лимитного входа зоной).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.analysis.base import MarketFeatures
from app.domain.models import Direction, Signal, Target, TradePlan
from app.scoring.scorer import ScoreResult

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PlanConfig:
    """Параметры планировщика (приезжают из риск-профиля настроек)."""

    stop_atr_mult: float = 1.5
    min_stop_atr: float = 0.9
    max_stop_atr: float = 4.5
    tp_atr_mults: Tuple[float, ...] = (1.5, 2.5, 4.0)
    risk_pct: float = 1.0
    anti_chase_atr: float = 2.0
    max_entry_distance_atr: float = 3.0
    entry_pullback_atr: float = 0.30
    horizon_hours: int = 24
    target_fractions: Tuple[float, ...] = (0.35, 0.35, 0.30)
    max_leverage: float = 3.0


def _atr_or_default(features: MarketFeatures) -> float:
    atr = features.atr
    if atr and atr == atr and atr > 0:
        return float(atr)
    price = features.price
    return price * 0.01 if price and price == price else float("nan")


# ---------------------------------------------------------------------------
# Зона входа
# ---------------------------------------------------------------------------

def _entry_zones(features: MarketFeatures, sign: int, price: float, atr: float,
                 cfg: PlanConfig) -> List[Tuple[float, float, float, str]]:
    """
    Кандидаты на зону входа: (low, high, вес, описание).

    Для лонга ищем зоны НИЖЕ цены (покупка отката), для шорта — выше.
    """
    zones: List[Tuple[float, float, float, str]] = []
    max_dist = cfg.max_entry_distance_atr * atr

    for struct in features.structure.values():
        for zone in struct.order_blocks:
            if zone.mitigated:
                continue
            if sign > 0 and not zone.is_bullish:
                continue
            if sign < 0 and zone.is_bullish:
                continue
            dist = (price - zone.high) if sign > 0 else (zone.low - price)
            if dist > max_dist or dist < -0.3 * atr:
                continue
            zones.append((zone.low, zone.high, 0.55 + 0.45 * zone.strength,
                          f"Order Block {zone.low:.6g}–{zone.high:.6g}"))
        for zone in struct.fvgs:
            if zone.mitigated:
                continue
            if sign > 0 and not zone.is_bullish:
                continue
            if sign < 0 and zone.is_bullish:
                continue
            dist = (price - zone.high) if sign > 0 else (zone.low - price)
            if dist > max_dist or dist < -0.3 * atr:
                continue
            zones.append((zone.low, zone.high, 0.45,
                          f"имбаланс {zone.low:.6g}–{zone.high:.6g}"))

    # Сильные уровни (свинги, POC, VAH/VAL, круглые числа).
    side = "support" if sign > 0 else "resistance"
    for lv in features.levels:
        if lv.kind != side:
            continue
        dist = (price - lv.price) if sign > 0 else (lv.price - price)
        if dist > max_dist or dist < -0.2 * atr:
            continue
        half = 0.18 * atr
        zones.append((lv.price - half, lv.price + half,
                      0.35 + 0.5 * lv.strength,
                      f"уровень {lv.price:.6g} ({lv.source})"))

    return zones


def build_entry(features: MarketFeatures, sign: int, price: float, atr: float,
                cfg: PlanConfig) -> Tuple[float, float, str]:
    """Возвращает (entry_low, entry_high, обоснование)."""
    zones = _entry_zones(features, sign, price, atr, cfg)
    if zones:
        def _rank(z: Tuple[float, float, float, str]) -> float:
            low, high, weight, _ = z
            mid = (low + high) / 2
            dist = abs(price - mid) / atr
            return weight - 0.12 * dist

        low, high, _, basis = max(zones, key=_rank)
        # Не «зализываем» зону: вход не выше цены + 0.15 ATR (для лонга).
        if sign > 0:
            high = min(high, price + 0.15 * atr)
            low = max(low, price - cfg.max_entry_distance_atr * atr)
        else:
            low = max(low, price - 0.15 * atr)
            high = min(high, price + cfg.max_entry_distance_atr * atr)
        if low > high:
            low, high = high, low
        return low, high, basis

    # Нет структурной зоны — небольшой лимитный «лестничный» вход у цены.
    if sign > 0:
        low = price - cfg.entry_pullback_atr * atr
        high = price + 0.05 * atr
        basis = "вход откатом к текущей цене (структурной зоны рядом нет)"
    else:
        low = price - 0.05 * atr
        high = price + cfg.entry_pullback_atr * atr
        basis = "вход отскоком к текущей цене (структурной зоны рядом нет)"
    return low, high, basis


# ---------------------------------------------------------------------------
# Стоп
# ---------------------------------------------------------------------------

def build_stop(features: MarketFeatures, sign: int, entry_low: float,
               entry_high: float, atr: float,
               cfg: PlanConfig) -> Tuple[float, str]:
    """Стоп за структурой с ATR-буфером и разумными границами."""
    entry_mid = (entry_low + entry_high) / 2
    edge = entry_low if sign > 0 else entry_high
    atr_stop = edge - sign * cfg.stop_atr_mult * atr

    # Структурный ориентир: ближайший свинг/зона ЗА краем входа.
    candidates: List[float] = []
    for struct in features.structure.values():
        for swing in struct.swings[-10:]:
            if sign > 0 and not swing.is_high and swing.price < edge:
                candidates.append(swing.price)
            if sign < 0 and swing.is_high and swing.price > edge:
                candidates.append(swing.price)
        for zone in struct.order_blocks:
            if zone.mitigated:
                continue
            if sign > 0 and zone.is_bullish and zone.low < edge:
                candidates.append(zone.low)
            if sign < 0 and not zone.is_bullish and zone.high > edge:
                candidates.append(zone.high)
    side = "support" if sign > 0 else "resistance"
    for lv in features.levels:
        if lv.kind != side or lv.strength < 0.4:
            continue
        if sign > 0 and lv.price < edge * 0.999:
            candidates.append(lv.price)
        if sign < 0 and lv.price > edge * 1.001:
            candidates.append(lv.price)

    basis = f"{cfg.stop_atr_mult:.1f}×ATR за точкой входа"
    stop = atr_stop
    if candidates:
        structural = max(candidates) if sign > 0 else min(candidates)
        buffered = structural - sign * 0.2 * atr
        # Берём более дальний из ATR-стопа и структурного, но в пределах лимита.
        if (sign > 0 and buffered < atr_stop) or (sign < 0 and buffered > atr_stop):
            stop = buffered
            basis = f"за структурой ({structural:.6g}) с буфером 0.2×ATR"

    # Ограничиваем риск: не дальше max_stop_atr и не ближе min_stop_atr.
    max_dist = cfg.max_stop_atr * atr
    min_dist = cfg.min_stop_atr * atr
    dist = abs(entry_mid - stop)
    if dist > max_dist:
        stop = entry_mid - sign * max_dist
        basis = f"ограничен {cfg.max_stop_atr:.1f}×ATR (структура слишком далеко)"
    elif dist < min_dist:
        stop = entry_mid - sign * min_dist
        basis = f"минимальный стоп {cfg.min_stop_atr:.1f}×ATR (иначе шум выбьет)"
    return stop, basis


# ---------------------------------------------------------------------------
# Цели
# ---------------------------------------------------------------------------

def _target_candidates(features: MarketFeatures, sign: int, entry_mid: float,
                       risk: float, atr: float) -> List[Tuple[float, float, str]]:
    """Кандидаты в цели: (цена, R, описание)."""
    out: List[Tuple[float, float, str]] = []

    side = "resistance" if sign > 0 else "support"
    for lv in features.levels:
        if lv.kind != side:
            continue
        dist = (lv.price - entry_mid) * sign
        if dist <= 0:
            continue
        r = dist / risk if risk else 0.0
        if r < 0.6 or r > 9:
            continue
        out.append((lv.price, r, f"{lv.source} {lv.price:.6g}"))

    for struct in features.structure.values():
        for lv in (struct.equal_highs if sign > 0 else struct.equal_lows):
            dist = (lv - entry_mid) * sign
            if dist <= 0:
                continue
            r = dist / risk if risk else 0.0
            if 0.6 <= r <= 9:
                out.append((lv, r, f"пул ликвидности {lv:.6g}"))

    vp = features.profile
    if vp is not None:
        for price, label in ((vp.poc, "POC"), (vp.vah, "VAH"), (vp.val, "VAL")):
            dist = (price - entry_mid) * sign
            if dist <= 0:
                continue
            r = dist / risk if risk else 0.0
            if 0.6 <= r <= 9:
                out.append((price, r, f"{label} {price:.6g}"))

    for tf, state in features.volatility.items():
        price = state.donchian_high if sign > 0 else state.donchian_low
        dist = (price - entry_mid) * sign
        if dist <= 0:
            continue
        r = dist / risk if risk else 0.0
        if 0.8 <= r <= 9:
            out.append((price, r, f"граница диапазона {tf.value}"))

    return out


def build_targets(features: MarketFeatures, sign: int, entry_mid: float, stop: float,
                  atr: float, cfg: PlanConfig) -> List[Target]:
    """Три цели: сначала реальные уровни, Недостающее добиваем ATR-целями."""
    risk = abs(entry_mid - stop)
    if risk <= 0:
        return []
    candidates = _target_candidates(features, sign, entry_mid, risk, atr)
    # ATR-цели как страховка, если структурных мало.
    for mult in cfg.tp_atr_mults:
        price = entry_mid + sign * mult * atr
        candidates.append((price, mult * atr / risk, f"{mult:.1f}×ATR"))

    candidates = [c for c in candidates if c[1] >= 0.9]
    candidates.sort(key=lambda c: c[1])

    # Убираем почти совпадающие цели.
    deduped: List[Tuple[float, float, str]] = []
    for price, r, basis in candidates:
        if deduped and abs(price - deduped[-1][0]) < 0.35 * atr:
            continue
        deduped.append((price, r, basis))

    targets: List[Target] = []
    fractions = cfg.target_fractions
    for idx, mult_floor in enumerate((1.1, 2.0, 3.2)):
        pick = None
        for price, r, basis in deduped:
            if r >= mult_floor and all(abs(price - t.price) > 0.35 * atr for t in targets):
                pick = (price, r, basis)
                break
        if pick is None:
            mult = cfg.tp_atr_mults[min(idx, len(cfg.tp_atr_mults) - 1)]
            if targets:
                mult = max(mult, (abs(targets[-1].price - entry_mid) / atr) + 1.0)
            price = entry_mid + sign * mult * atr
            pick = (price, abs(price - entry_mid) / risk, f"{mult:.1f}×ATR")
        targets.append(Target(price=pick[0], label=f"TP{idx + 1}",
                              basis=pick[2], fraction=fractions[idx]
                              if idx < len(fractions) else 0.3))
    return targets


# ---------------------------------------------------------------------------
# Сборка плана
# ---------------------------------------------------------------------------

def build_plan(features: MarketFeatures, direction: Direction,
               cfg: PlanConfig) -> Optional[TradePlan]:
    """Собирает план сделки. Возвращает None, если построить невозможно."""
    if direction is Direction.WAIT:
        return None
    price = features.price
    atr = _atr_or_default(features)
    if not (price == price and atr == atr) or price <= 0 or atr <= 0:
        return None

    sign = direction.sign
    entry_low, entry_high, entry_basis = build_entry(features, sign, price, atr, cfg)
    stop, stop_basis = build_stop(features, sign, entry_low, entry_high, atr, cfg)
    entry_mid = (entry_low + entry_high) / 2
    targets = build_targets(features, sign, entry_mid, stop, atr, cfg)
    if not targets:
        return None

    plan = TradePlan(
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        targets=targets,
        entry_basis=entry_basis,
        stop_basis=stop_basis,
        invalidation=stop,
        horizon_hours=cfg.horizon_hours,
        atr=atr,
        risk_pct_of_deposit=cfg.risk_pct,
        max_leverage=cfg.max_leverage,
    )
    if not plan.is_valid():
        log.debug("%s: план не прошёл самопроверку", features.symbol)
        return None
    return plan


def plan_from_config(settings) -> PlanConfig:
    """Собирает PlanConfig из настроек бота (риск-профиль)."""
    preset = settings.preset()
    return PlanConfig(
        stop_atr_mult=float(preset.get("stop_atr_mult", 1.5)),
        tp_atr_mults=tuple(preset.get("tp_atr_mults", (1.5, 2.5, 4.0))),
        risk_pct=float(settings.risk_per_trade_pct),
        anti_chase_atr=float(settings.anti_chase_atr),
        horizon_hours=int(preset.get("horizon_hours", 24)),
        max_leverage=float(settings.max_leverage),
    )
