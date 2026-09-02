"""
app/analysis/modules/sentiment.py — новостной/социальный фон.

Самая шумная группа факторов, поэтому вес минимальный (0.40 в GROUP_WEIGHTS),
а модуль вообще ничего не выдаёт, если данных нет. Новость «биржу взломали»
может сломать любую техническую картину — игнорировать её нельзя, но и
торговать заголовки в одиночку тоже.
"""

from __future__ import annotations

from typing import Iterable

from app.analysis.base import Group, MarketFeatures
from app.analysis.registry import analysis_module
from app.analysis.utils import clamp
from app.domain.models import Factor


@analysis_module("news_sentiment", Group.SENTIMENT,
                 description="Тональность новостного фона по монете")
def news_sentiment(features: MarketFeatures) -> Iterable[Factor]:
    sentiment = features.snapshot.sentiment
    if sentiment is None:
        return []
    score = clamp(sentiment)
    if abs(score) < 0.1:
        return []
    headlines = features.snapshot.news[:2]
    extra = f" — «{headlines[0]}»" if headlines else ""
    tone = "позитивный" if score > 0 else "негативный"
    yield Factor(
        "news_sentiment", Group.SENTIMENT, score,
        f"новостной фон {tone} ({score:+.2f}){extra}",
        weight=1.0,
    )
