"""
Matcher — подбор монет под запрос пользователя (UserRequest).

Это «сердце» фичи «найди монеты по моему запросу». Для каждого кандидата:
1) жёсткие фильтры (ликвидность, волатильность, анти-гонка);
2) скоринг технической картины (стратегия);
3) ранжирование по `match_score` — чем лучше монета подходит под запрос,
   тем выше балл.

Результат — список Match с коротким «почему подошла».
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from ..config.profiles import get_profile
from ..core.domain.query import UserRequest
from ..core.domain.signal import Direction
from ..data.exchange import TickerMeta
from ..strategy.base import ACTIVE_GROUP_THRESHOLD, IStrategy
from .scorer import score_data

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Match:
    """Монета, подошедшая под запрос."""

    symbol: str
    exchange: str
    last_price: float
    volume_24h: float
    change_24h: float                            # реальное изменение цены за 24ч (%)
    direction: Direction
    signal_confidence: float
    data_confidence: float
    match_score: float          # 0..100 — насколько подходит под запрос
    atr_pct: float
    group_scores: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    rejected_reason: str = ""

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0] if "/" in self.symbol else self.symbol


class Matcher:
    """Ранжирует кандидатов под UserRequest."""

    def __init__(self, strategy: IStrategy) -> None:
        self.strategy = strategy

    def match(
        self,
        meta: TickerMeta,
        df: pd.DataFrame,
        request: UserRequest,
        deep: bool = False,
    ) -> Optional[Match]:
        """Вернёт Match или None, если монета не прошла жёсткие фильтры.

        `deep=True` — анализ конкретной монеты по просьбе пользователя:
        жёсткие тикер-фильтры (объём/спред) пропускаются, важна только
        техническая картина (направление + согласие факторов).
        """
        profile = get_profile(request.risk_profile)

        # Индикаторы + групповые сигналы (enrich один раз).
        df_enriched = self.strategy.populate_indicators(df) if "trend_signal_raw" not in df.columns else df

        # ── Жёсткие фильтры (только для массового подбора) ─────
        if not deep:
            if meta.quote_volume_24h < max(request.min_volume_usd_24h, profile.min_volume_usd_24h):
                return None
            if meta.last_price <= 0:
                return None
            if meta.spread_pct > 0.5:
                return None

            atr_pct = float(df_enriched.get("atr_pct", pd.Series([1.0])).iloc[-1]) if "atr_pct" in df_enriched.columns else 1.0
            if atr_pct < max(request.min_atr_pct, profile.min_atr_pct) or atr_pct > min(request.max_atr_pct, profile.max_atr_pct):
                return self._rejected(meta, df_enriched, f"ATR% {atr_pct:.2f} вне диапазона")

            if not self._anti_chase_ok(df_enriched):
                return self._rejected(meta, df_enriched, "анти-гонка (движение уже состоялось)")

        # ── Техническая картина ────────────────────────────────
        atr_pct = float(df_enriched.get("atr_pct", pd.Series([1.0])).iloc[-1]) if "atr_pct" in df_enriched.columns else 1.0
        group_scores = self.strategy.group_scores(df_enriched)
        direction = self.strategy.decide_direction(group_scores)
        req_dir = request.wants_direction
        if req_dir is not None:
            direction = req_dir

        if direction == Direction.NEUTRAL:
            return self._rejected(meta, df_enriched, "нет выраженного направления")

        signal_conf = self.strategy.confidence(group_scores, direction)
        floor = profile.min_confidence if not deep else 40.0
        if signal_conf < floor:
            return self._rejected(meta, df_enriched,
                                  f"уверенность {signal_conf:.0f}% < {floor:.0f}%")

        data_conf = score_data(df_enriched)
        keyword_hit = self._keyword_hit(meta.base, request)

        score, reasons = self._score_match(
            meta, request, profile, direction, signal_conf, atr_pct, keyword_hit, group_scores,
        )

        return Match(
            symbol=meta.symbol,
            exchange=meta.exchange,
            last_price=meta.last_price,
            volume_24h=meta.quote_volume_24h,
            change_24h=meta.change_pct_24h,
            direction=direction,
            signal_confidence=signal_conf,
            data_confidence=data_conf,
            match_score=round(score, 1),
            atr_pct=round(atr_pct, 2),
            group_scores=group_scores,
            reasons=reasons,
        )

    def _score_match(
        self,
        meta: TickerMeta,
        request: UserRequest,
        profile,
        direction: Direction,
        signal_conf: float,
        atr_pct: float,
        keyword_hit: bool,
        group_scores: Dict[str, float],
    ) -> tuple[float, List[str]]:
        """Композиция балла: техника + ликвидность + соответствие профилю."""
        reasons: List[str] = []
        sign = 1.0 if direction == Direction.LONG else -1.0

        tech = min(signal_conf, 100.0) * 0.5
        reasons.append(f"уверенность {signal_conf:.0f}% за {direction.value}")

        vol = meta.quote_volume_24h
        vol_score = min(40.0, 40.0 * (vol / max(profile.min_volume_usd_24h * 3, 1.0)))
        if vol >= profile.min_volume_usd_24h * 3:
            reasons.append("высокая ликвидность")
        elif vol >= profile.min_volume_usd_24h:
            reasons.append("достаточная ликвидность")

        if profile.appetite <= 0.2:
            vol_fit = 20.0 if atr_pct <= 3.0 else max(0.0, 20.0 - (atr_pct - 3.0) * 5)
        elif profile.appetite >= 0.8:
            vol_fit = 20.0 if atr_pct >= 2.0 else 12.0
        else:
            vol_fit = 20.0 if 1.0 <= atr_pct <= 6.0 else 10.0

        boost = 8.0 if keyword_hit else 0.0
        if keyword_hit:
            reasons.append("совпадает с темой запроса")

        agreeing = [g for g, s in group_scores.items()
                    if abs(s) >= ACTIVE_GROUP_THRESHOLD and s * sign > 0]
        breadth_score = min(10.0, len(agreeing) * 2.5)
        if len(agreeing) >= 3:
            reasons.append(f"{len(agreeing)} группы подтверждают")

        return min(99.0, tech + vol_score + vol_fit + boost + breadth_score), reasons

    @staticmethod
    def _rejected(meta: TickerMeta, df: pd.DataFrame, reason: str) -> "Match":
        """Возвращает Match с rejected_reason (для статистики/логов)."""
        group_scores = Matcher._empty_scores()
        atr_pct = float(df.get("atr_pct", pd.Series([1.0])).iloc[-1]) if "atr_pct" in df.columns else 1.0
        return Match(
            symbol=meta.symbol, exchange=meta.exchange, last_price=meta.last_price,
            volume_24h=meta.quote_volume_24h, change_24h=meta.change_pct_24h,
            direction=Direction.NEUTRAL,
            signal_confidence=0.0, data_confidence=0.0, match_score=0.0,
            atr_pct=round(atr_pct, 2), group_scores=group_scores,
            reasons=[], rejected_reason=reason,
        )

    @staticmethod
    def _empty_scores() -> Dict[str, float]:
        from ..indicators import ALL_GROUPS
        return {g: 0.0 for g in ALL_GROUPS}

    @staticmethod
    def _keyword_hit(base: str, request: UserRequest) -> bool:
        if request.keyword:
            kws = request.keyword.lower().split()
            if any(kw in base.lower() for kw in kws):
                return True
        if request.symbols and base.upper() in [s.upper() for s in request.symbols]:
            return True
        return False

    @staticmethod
    def _anti_chase_ok(df: pd.DataFrame) -> bool:
        window = 12
        if len(df) < window + 1:
            return True
        w = df["close"].iloc[-window - 1:]
        if w.empty:
            return True
        change = (w.iloc[-1] / w.iloc[0] - 1) * 100
        return abs(change) <= 8.0
