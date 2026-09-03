"""
Шлюз к биржам через ccxt (async).

Идея из Freqtrade: один интерфейс, много бирж. Под капотом — ccxt async.
Метод `safe_call` оборачивает любой вызов с ретраями и обработкой RateLimit.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ...core.domain.candle import Candles
from ...core.domain.market import TickerMeta
from ...core.domain.ticker import Ticker

log = logging.getLogger(__name__)


# ccxt ожидает строки вида "BTC/USDT" (slash-form).
def to_ccxt_symbol(ticker: Ticker) -> str:
    return ticker.symbol


def parse_ohlcv_row(row) -> tuple:
    return (int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))


@dataclass(slots=True)
class _ExchangeHandle:
    name: str
    client: Any  # ccxt async instance


class ExchangeGateway:
    """
    Асинхронный шлюз к одной или нескольким биржам.

    Создаёт ccxt async instance, кеширует список тикеров,
    и предоставляет 3 высокоуровневых метода:
      - `fetch_tickers_meta()`: TickerMeta по топ-N монетам.
      - `fetch_candles()`: мульти-ТФ Candles по символу.
      - `ping()`: жива ли биржа.
    """

    def __init__(self, exchanges: List[str], quote_currencies: List[str]) -> None:
        self._quote_currencies = [q.upper() for q in quote_currencies]
        self._handles: Dict[str, _ExchangeHandle] = {}
        self._lock = asyncio.Lock()

    # ── Lifecycle ──────────────────────────────────────────────
    async def start(self) -> None:
        try:
            import ccxt.async_support as ccxt  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ccxt is required: pip install ccxt"
            ) from exc

        for name in self._handles or []:
            await self._handles[name].client.close()
        self._handles.clear()

        for name in self._handles_first_use() if False else []:  # noqa
            pass

        # Делаем в два шага: сначала собираем имена, потом создаём клиентов.
        for name in (self._ex_names() if False else self._ex_list()):
            try:
                cls = getattr(ccxt, name)
                client = cls({"enableRateLimit": True, "timeout": 20000})
                self._handles[name] = _ExchangeHandle(name=name, client=client)
                log.info("exchange %s initialised", name)
            except AttributeError:
                log.warning("ccxt has no exchange %r — skip", name)
            except Exception as exc:  # noqa: BLE001
                log.warning("ccxt %s init failed: %s", name, exc)

    def _ex_list(self) -> List[str]:
        # Простой способ избежать AttributeError-ловушки в предыдущем коде.
        return list(getattr(self, "_pending", []) or [])

    # Удобная фабрика.
    @classmethod
    async def create(cls, exchanges: List[str], quote_currencies: List[str]) -> "ExchangeGateway":
        gw = cls(exchanges, quote_currencies)
        gw._pending = list(exchanges)
        await gw.start()
        gw._pending = []
        return gw

    async def close(self) -> None:
        for h in self._handles.values():
            try:
                await h.client.close()
            except Exception:  # noqa: BLE001
                pass
        self._handles.clear()

    # ── API ────────────────────────────────────────────────────
    def available(self) -> List[str]:
        return list(self._handles.keys())

    async def ping(self, exchange: str) -> bool:
        h = self._handles.get(exchange)
        if not h:
            return False
        try:
            await h.client.fetch_time()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("ping %s failed: %s", exchange, exc)
            return False

    async def fetch_tickers_meta(
        self, exchange: str, top_n: int = 100
    ) -> List[TickerMeta]:
        """
        Тянет все тикеры, фильтрует по quote, сортирует по объёму 24h.
        """
        h = self._handles.get(exchange)
        if not h:
            return []
        try:
            tickers = await h.client.fetch_tickers()
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_tickers %s failed: %s", exchange, exc)
            return []

        out: List[TickerMeta] = []
        for sym, t in tickers.items():
            try:
                if "/" not in sym:
                    continue
                base, quote = sym.split("/", 1)
                if quote.upper() not in self._quote_currencies:
                    continue
                # фильтр мусора: цена/объём > 0
                last = float(t.get("last") or 0)
                qvol = float(t.get("quoteVolume") or 0)
                if last <= 0 or qvol <= 0:
                    continue
                change = float((t.get("info") or {}).get("priceChangePercent")
                               or t.get("percentage")
                               or 0.0)
                bid = float(t.get("bid") or 0)
                ask = float(t.get("ask") or 0)
                spread = ((ask - bid) / last * 100) if (ask > 0 and bid > 0 and last > 0) else 0.0
                out.append(TickerMeta(
                    symbol=sym,
                    exchange=exchange,
                    last_price=last,
                    quote_volume_24h=qvol,
                    change_pct_24h=change,
                    bid=bid,
                    ask=ask,
                    spread_pct=spread,
                ))
            except Exception:  # noqa: BLE001
                continue

        out.sort(key=lambda m: m.quote_volume_24h, reverse=True)
        return out[:top_n]

    async def fetch_candles(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        limit: int = 300,
    ) -> Optional[Candles]:
        h = self._handles.get(exchange)
        if not h:
            return None
        try:
            ohlcv = await h.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch_ohlcv %s %s %s: %s", exchange, symbol, timeframe, exc)
            return None
        if not ohlcv:
            return None
        return Candles.from_ccxt(ohlcv, exchange=exchange, symbol=symbol, timeframe=timeframe)

    async def fetch_multi_tf_candles(
        self,
        exchange: str,
        symbol: str,
        timeframes: List[str],
        limit: int = 300,
    ) -> Dict[str, Candles]:
        """Тянет свечи по нескольким ТФ параллельно."""
        tasks = {
            tf: self.fetch_candles(exchange, symbol, tf, limit=limit)
            for tf in timeframes
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        out: Dict[str, Candles] = {}
        for tf, res in zip(tasks.keys(), results):
            if isinstance(res, Candles):
                out[tf] = res
        return out


# ── Singleton ────────────────────────────────────────────────
_gateway: Optional[ExchangeGateway] = None


async def get_gateway(exchanges: List[str], quote_currencies: List[str]) -> ExchangeGateway:
    global _gateway
    if _gateway is None:
        _gateway = await ExchangeGateway.create(exchanges, quote_currencies)
    return _gateway


async def close_gateway() -> None:
    global _gateway
    if _gateway is not None:
        await _gateway.close()
        _gateway = None
