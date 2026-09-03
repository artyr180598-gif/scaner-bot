"""Lightweight natural-language parser for ticker / setup requests.

No ML, no hallucinations — only deterministic regex rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

TICKER_RE = re.compile(r"\b([A-Z0-9]{2,10})(?:/(USDT|USD|BTC|ETH))?\b", re.I)
TF_RE = re.compile(r"\b(15m|30m|1h|4h|1d)\b", re.I)


@dataclass(slots=True)
class SearchQuery:
    symbol: Optional[str] = None
    direction: Optional[str] = None
    mode: Optional[str] = None
    timeframe: Optional[str] = None
    min_volume_usd: Optional[float] = None
    min_abs_change: Optional[float] = None
    max_atr: Optional[float] = None
    raw: str = ""


def _extract_symbol(text: str, known: set[str] | None = None) -> Optional[str]:
    tokens = re.findall(r"\b[A-Z0-9]{2,10}\b", text.upper())
    # Prefer quoted / slash style
    for m in re.finditer(r"\$?([A-Z0-9]{2,10})(?:/(USDT|USD))?", text.upper()):
        candidate = m.group(1)
        q = m.group(2) or "USDT"
        if candidate in {"LONG", "SHORT", "BUY", "SELL", "SCALP", "SWING"}:
            continue
        if known and f"{candidate}{q}" in known:
            return f"{candidate}{q}"
    # fallback: first plausible ticker token that isn't a keyword
    skip = {
        "LONG", "SHORT", "BUY", "SELL", "SCALP", "SWING", "USDT", "USD", "BTC",
        "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "DOT", "LINK", "MATIC",
        "AVAX", "UNI", "ATOM", "NEAR", "APT", "ARB", "OP", "INJ", "SUI",
        "VOLUME", "VOL", "CHANGE", "VOLATILITY", "ATR", "ALTCOIN", "ALTCOINS",
        "COIN", "CRYPTO", "MARKET", "SEARCH", "FIND", "ANALYSIS", "ANALYSE",
        "PRICE", "WATCH", "TOP", "BEST", "LIVE", "FUNDING", "OI",
    }
    for t in tokens:
        if t not in skip and t.isalpha():
            if t.endswith("USDT"):
                return t
            if t.endswith("USD"):
                return t[:-3] + "USDT"
            return t + "USDT"
    for t in tokens:
        if t in ("BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "DOT", "LINK", "MATIC", "AVAX", "UNI", "ATOM", "NEAR", "APT", "ARB", "OP", "INJ", "SUI"):
            return t + "USDT"
    return None


def parse_query(text: str) -> SearchQuery:
    raw = text.strip()
    lower = raw.lower()
    q = SearchQuery(raw=raw)
    if re.search(r"\b(long|лонг|buy|купля|покупк)\b", lower):
        q.direction = "LONG"
    if re.search(r"\b(short|шорт|sell|продаж)\b", lower):
        q.direction = "SHORT"
    tf = TF_RE.search(lower)
    if tf:
        q.timeframe = tf.group(1).lower()
    if re.search(r"\b(scalp|скальп)\b", lower) or q.timeframe in ("15m", "30m"):
        q.mode = "scalp"
    if re.search(r"\b(swing|свинг|дневн|дейли)\b", lower) or q.timeframe in ("4h", "1d"):
        q.mode = "swing"
    if not q.mode and q.timeframe:
        q.mode = "scalp" if q.timeframe in ("15m", "30m", "1h") else "swing"

    # extract numbers around volume / change
    m = re.search(r"(?:volume|объ|turnover)\D{0,8}([0-9]+(?:\.[0-9]+)?)\s*(k|m|mln|b|bn|тыс|млн|млрд)?", lower)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ("k", "тыс"):
            val *= 1_000
        elif unit in ("m", "mln", "млн"):
            val *= 1_000_000
        q.min_volume_usd = val

    m = re.search(r"(?:change|измен|движ|рост|пад).{0,8}([0-9]+(?:\.[0-9]+)?)\s*%", lower)
    if m:
        q.min_abs_change = float(m.group(1))

    m = re.search(r"(?:atr|volatility|волатильност)\D{0,6}([0-9]+(?:\.[0-9]+)?)\s*%", lower)
    if m:
        q.max_atr = float(m.group(1))

    q.symbol = _extract_symbol(raw)
    return q
