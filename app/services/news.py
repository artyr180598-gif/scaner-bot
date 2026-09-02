"""
app/services/news.py — новостной фон (опциональный и деградируемый).

Источник по умолчанию — публичный новостной API CryptoCompare (работает без
ключа, ключ лишь поднимает лимиты). Модуль полностью опционален: если сеть
недоступна или ключа нет — возвращает пустой результат, и анализ честно идёт
без новостного фактора (лучше «нет данных», чем выдуманный сентимент).

Оценка тональности — простой словарный метод: без внешних ML-моделей и без
зависимостей. Для криптоновостей этого достаточно, чтобы отличить «SEC
approved ETF» от «exchange hacked».
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.data.cache import TTLCache

log = logging.getLogger(__name__)

POSITIVE = {
    "approval", "approved", "adopt", "adoption", "partnership", "launch",
    "launches", "integration", "upgrade", "breakout", "surge", "rally",
    "record high", "ath", "etf inflow", "inflows", "accumulate", "buyback",
    "listing", "halving", "mainnet", "milestone", "bullish",
    "одобрение", "одобрен", "партнёрство", "запуск", "интеграция", "рост",
    "рекорд", "приток", "листинг", "апгрейд",
}
NEGATIVE = {
    "hack", "hacked", "exploit", "scam", "fraud", "lawsuit", "sues", "sued",
    "sec charges", "ban", "banned", "crash", "plunge", "dump", "liquidation",
    "bankrupt", "insolvency", "outflow", "outflows", "delist", "delisting",
    "vulnerability", "rug", "bearish", "warning",
    "взлом", "мошенничество", "иск", "запрет", "обвал", "банкротство",
    "отток", "делистинг", "уязвимость",
}


@dataclass(slots=True)
class NewsItem:
    title: str
    source: str = ""
    url: str = ""
    published_at: float = 0.0
    score: float = 0.0


@dataclass(slots=True)
class SentimentResult:
    symbol_base: str
    score: float                       # -1..1
    items: List[NewsItem] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.score > 0.25:
            return "позитивный"
        if self.score < -0.25:
            return "негативный"
        return "нейтральный"


class NewsService:
    """Заголовки новостей по монете + словарная оценка тональности."""

    def __init__(self, api_key: str = "", enabled: bool = True,
                 ttl_seconds: int = 900, timeout: float = 8.0) -> None:
        self.api_key = api_key
        self.enabled = enabled
        self.timeout = timeout
        self.cache = TTLCache(ttl_seconds, maxsize=512)
        self.errors = 0

    # ------------------------------------------------------------------
    async def sentiment(self, base: str) -> Optional[SentimentResult]:
        """Возвращает SentimentResult или None, если источник недоступен."""
        if not self.enabled:
            return None
        base = base.upper()

        async def _produce() -> Optional[SentimentResult]:
            items = await self._fetch(base)
            if not items:
                return None
            scored = [NewsItem(i.title, i.source, i.url, i.published_at,
                               score_text(i.title)) for i in items]
            relevant = [i for i in scored if i.score != 0]
            if not relevant:
                return SentimentResult(base, 0.0, scored[:5])
            total = sum(i.score for i in relevant)
            # Знаменатель — ВСЕ заголовки, а не только эмоциональные:
            # иначе две новости из двух давали бы ±1.0 и перевешивали технику.
            score = max(-1.0, min(1.0, total / max(3, len(scored))))
            return SentimentResult(base, score,
                                   sorted(scored, key=lambda i: -abs(i.score))[:5])

        return await self.cache.get_or_set(base, _produce)

    async def sentiment_many(self, bases: Sequence[str]
                             ) -> Dict[str, SentimentResult]:
        if not self.enabled or not bases:
            return {}
        results = await asyncio.gather(*[self.sentiment(b) for b in bases],
                                       return_exceptions=True)
        out: Dict[str, SentimentResult] = {}
        for base, res in zip(bases, results):
            if isinstance(res, SentimentResult):
                out[base.upper()] = res
            elif isinstance(res, BaseException):
                self.errors += 1
        return out

    # ------------------------------------------------------------------
    async def _fetch(self, base: str) -> List[NewsItem]:
        try:
            import httpx
        except ImportError:  # pragma: no cover
            log.debug("httpx не установлен — новостной фон отключён")
            return []
        url = "https://min-api.cryptocompare.com/data/v2/news/"
        params = {"lang": "EN", "categories": base}
        if self.api_key:
            params["api_key"] = self.api_key
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            log.debug("новости недоступны (%s)", exc)
            return []

        items: List[NewsItem] = []
        for raw in (payload.get("Data") or [])[:40]:
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            items.append(NewsItem(
                title=title,
                source=str(raw.get("source") or ""),
                url=str(raw.get("url") or ""),
                published_at=float(raw.get("published_on") or time.time()),
            ))
        return items


_WORD = re.compile(r"[a-zа-яё]+", re.I)


def score_text(text: str) -> float:
    """Словарная тональность заголовка в [-1, 1]."""
    if not text:
        return 0.0
    lowered = text.lower()
    words = set(_WORD.findall(lowered))
    pos = len(words & POSITIVE) + sum(1 for p in POSITIVE if " " in p and p in lowered)
    neg = len(words & NEGATIVE) + sum(1 for n in NEGATIVE if " " in n and n in lowered)
    if pos == neg:
        return 0.0
    total = pos + neg
    return (pos - neg) / total
