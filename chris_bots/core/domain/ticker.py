"""Тикер — нормализованное представление монеты на бирже."""
from __future__ import annotations

import re
from dataclasses import dataclass


# Whitelist допустимых символов в тикере (защита от мусора и нестандарта).
_TICKER_RE = re.compile(r"^[A-Z0-9]{2,20}/?[A-Z0-9]{2,20}$")


@dataclass(slots=True, frozen=True)
class Ticker:
    """Нормализованный тикер: BTC/USDT (quote всегда отделена /)."""

    base: str   # BTC
    quote: str  # USDT
    exchange: str = ""

    def __str__(self) -> str:
        return f"{self.base}/{self.quote}"

    @property
    def symbol(self) -> str:
        return str(self)

    @classmethod
    def parse(cls, raw: str, exchange: str = "") -> "Ticker":
        """
        Парсит тикер в разных форматах: 'BTCUSDT', 'BTC/USDT', 'BTC-USDT'.
        Бросает ValueError для мусора.
        """
        s = (raw or "").strip().upper().replace("-", "/").replace("_", "/")
        if "/" in s:
            base, quote = s.split("/", 1)
        else:
            # Пытаемся выделить котировку (USDT/USDC/BUSD/USDD/TUSD/FDUSD/DAI/EUR/USD/BTC/ETH).
            for q in ("USDT", "USDC", "BUSD", "USDD", "TUSD", "FDUSD", "DAI", "EUR", "USD", "BTC", "ETH"):
                if s.endswith(q) and len(s) > len(q):
                    base, quote = s[: -len(q)], q
                    break
            else:
                raise ValueError(f"cannot parse ticker from {raw!r}")
        if not _TICKER_RE.match(f"{base}/{quote}"):
            raise ValueError(f"invalid ticker: {raw!r}")
        return cls(base=base, quote=quote, exchange=exchange)
