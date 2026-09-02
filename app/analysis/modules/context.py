"""
app/analysis/modules/context.py — общий фон рынка: BTC и ширина рынка.

Альт-монеты в 80% случаев следуют за BTC. Игнорировать это — значит ловить
лонги в альткоинах на падающем рынке. Модуль добавляет контекстный фактор и
не влияет на сам BTC.
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp
from app.domain.models import Factor


def _is_btc(symbol: str) -> bool:
    base = symbol.split("/")[0].upper()
    return base in {"BTC", "WBTC", "BTCB", "XBT"}


@analysis_module("btc_alignment", Group.CONTEXT,
                 description="Направление BTC относительно сигнала по альткоину")
def btc_alignment(features: MarketFeatures) -> Iterable[Factor]:
    if _is_btc(features.symbol):
        return []
    ctx = features.context
    if abs(ctx.btc_score) < 0.1 and ctx.btc_trend == "флэт":
        return []
    score = clamp(ctx.btc_score * 0.8)
    yield Factor(
        "btc_alignment", Group.CONTEXT, score,
        f"BTC {ctx.btc_trend} (оценка {ctx.btc_score:+.2f}) — "
        f"{'фон помогает' if score > 0 else ('фон мешает' if score < 0 else 'нейтрально')}",
        weight=0.9,
    )


@analysis_module("market_breadth", Group.CONTEXT,
                 description="Ширина рынка: сколько монет участвует в движении")
def market_breadth(features: MarketFeatures) -> Iterable[Factor]:
    ctx = features.context
    breadth = ctx.breadth_24h_positive
    if not breadth:
        return []
    score = clamp((breadth - 0.5) * 1.6)
    if abs(score) < 0.12:
        return []
    tone = "рынок растёт широким фронтом" if score > 0 else "рынок под давлением, рост узкий"
    yield Factor("market_breadth", Group.CONTEXT, score,
                 f"{breadth * 100:.0f}% монет в плюсе за 24ч — {tone}", weight=0.6)
