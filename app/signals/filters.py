"""
app/signals/filters.py — гейты качества сигнала.

Продуктовый принцип: лучше ноль сигналов, чем мусорный сигнал. Каждый гейт
возвращает понятную причину отказа — она попадает в журнал и в интерфейс
(пользователь видит «почему WAIT», а не пустоту).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from app.analysis.base import MarketFeatures
from app.domain.models import Direction, TradePlan
from app.scoring.scorer import ScoreResult


@dataclass(slots=True)
class FilterConfig:
    min_confidence: float = 6.0
    min_rr: float = 1.8
    min_potential: float = 0.30
    min_agreement: int = 2
    min_data_confidence: float = 0.55
    anti_chase_atr: float = 2.0
    min_quote_volume_usd: float = 3_000_000
    max_staleness_seconds: float = 900
    max_stop_pct: float = 12.0


@dataclass(slots=True)
class FilterResult:
    accepted: bool
    direction: Direction
    reasons: List[str] = field(default_factory=list)
    hard: bool = True          # False = «почти сигнал», можно показать как WATCH

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "принято"


def apply_filters(
    score: ScoreResult,
    plan: Optional[TradePlan],
    features: MarketFeatures,
    cfg: FilterConfig,
) -> FilterResult:
    """Прогоняет сигнал через все гейты. Порядок важен: от дешёвых к дорогим."""
    reasons: List[str] = []
    hard = True

    if score.direction is Direction.WAIT:
        return FilterResult(False, Direction.WAIT,
                            score.notes or ["аргументы противоречат друг другу"])

    if score.agreement < cfg.min_agreement:
        reasons.append(
            f"направление поддерживает {score.agreement} независимая группа "
            f"из нужных {cfg.min_agreement}")
    if score.confidence < cfg.min_confidence:
        reasons.append(
            f"уверенность {score.confidence:.1f}/10 ниже порога {cfg.min_confidence:.1f}")
    if score.potential < cfg.min_potential:
        reasons.append(
            f"потенциал движения {score.potential * 100:.0f}% — монета может "
            f"остаться в боковике")
    if score.data_confidence < cfg.min_data_confidence:
        reasons.append(f"качество данных {score.data_confidence * 100:.0f}% — "
                       f"анализу нельзя доверять")

    ticker = features.ticker
    if ticker is not None and ticker.quote_volume < cfg.min_quote_volume_usd:
        reasons.append(f"оборот {ticker.quote_volume / 1e6:.1f}M$ ниже порога "
                       f"{cfg.min_quote_volume_usd / 1e6:.0f}M$ — неликвид")

    staleness = features.snapshot.staleness_seconds()
    if staleness > cfg.max_staleness_seconds:
        reasons.append(f"данные устарели на {staleness / 60:.0f} мин")

    if plan is None:
        reasons.append("не удалось построить план сделки (нет вменяемой зоны/стопа)")
        return FilterResult(False, score.direction, reasons)

    # Порог R:R проверяем по СРЕДНЕЙ цели: TP1 намеренно ставят близко
    # (частичная фиксация), поэтому судить по ней одной — значит отбраковать
    # нормальные планы с далёкими TP2/TP3.
    if plan.rr_avg < cfg.min_rr:
        reasons.append(f"средний R:R {plan.rr_avg:.1f} ниже порога {cfg.min_rr:.1f}")
    if plan.rr_primary < 1.0:
        reasons.append(f"R:R до первой цели {plan.rr_primary:.1f} меньше 1 — "
                       f"цель ближе стопа")
    if abs(plan.stop_pct) > cfg.max_stop_pct:
        reasons.append(f"стоп {plan.stop_pct:+.1f}% — слишком широкий риск")

    # Анти-погоня: если цена уже ушла от зоны входа — это не вход, а догонялки.
    atr = plan.atr if plan.atr == plan.atr else 0.0
    if atr:
        price = features.price
        if plan.direction is Direction.LONG:
            distance = (price - plan.entry_high) / atr
        else:
            distance = (plan.entry_low - price) / atr
        if distance > cfg.anti_chase_atr:
            reasons.append(
                f"цена ушла от зоны входа на {distance:.1f} ATR — ждём откат, "
                f"не догоняем")
            hard = False

    if reasons and hard:
        return FilterResult(False, Direction.WAIT, reasons)
    if reasons:
        return FilterResult(False, score.direction, reasons, hard=False)
    return FilterResult(True, score.direction, ["все гейты пройдены"])
