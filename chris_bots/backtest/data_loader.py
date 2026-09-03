"""
Загрузчик исторических данных OKX через публичный REST API.

OKX доступен из песочницы (см. BRAIN.md п.8). Не требует API-ключей.
Использует инструмент `fetch_page` платформы Arena.ai — он умеет OKX
и Bitget, в отличие от прямого HTTPS из sandbox.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from typing import List, Optional

from ..core.domain.candle import Candle

log = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"


def _http_get(url: str, timeout: float = 30.0) -> dict:
    """Минималистичный GET без зависимостей (для прод-окружения)."""
    req = urllib.request.Request(url, headers={"User-Agent": "chris-bots/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def _fetch_page_proxy(url: str) -> Optional[dict]:
    """
    Адаптер для fetch_page инструмента платформы (используется в sandbox).

    Если доступен глобальный хук `arena_fetch_page` — вызывает его.
    Иначе возвращает None.
    """
    fetcher = globals().get("arena_fetch_page")
    if fetcher is None:
        # Попробуем достать из ENV-маркера (платформа может установить его).
        env = os.environ.get("ARENA_FETCH_PAGE_AVAILABLE")
        if env == "1":
            # Если платформа говорит, что fetch_page доступен — но без хука,
            # мы не можем его вызвать. Возвращаем None.
            return None
        return None
    try:
        result = fetcher(url)
        if isinstance(result, str):
            return json.loads(result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_page proxy failed: %s", exc)
        return None


def fetch_okx_candles(
    inst_id: str = "BTC-USDT",
    bar: str = "1H",
    limit: int = 300,
) -> List[Candle]:
    """
    Тянет последние N свечей OKX.

    Args:
        inst_id: инструмент (BTC-USDT, ETH-USDT, …)
        bar: таймфрейм (1m, 5m, 15m, 1H, 4H, 1D, …)
        limit: сколько свечей (max 300 за раз)

    Returns:
        Список Candle, отсортированный по времени (старые → новые).
    """
    params = urllib.parse.urlencode({"instId": inst_id, "bar": bar, "limit": str(limit)})
    url = f"{OKX_BASE}/api/v5/market/history-candles?{params}"
    log.info("GET %s", url)
    payload: Optional[dict] = None
    # 1) Пробуем прямой HTTPS (прод).
    try:
        payload = _http_get(url)
    except Exception as exc:  # noqa: BLE001
        log.debug("direct HTTPS failed, trying proxy: %s", exc)
    # 2) Пробуем через fetch_page (sandbox).
    if payload is None:
        payload = _fetch_page_proxy(url)
    if payload is None:
        log.warning("OKX fetch failed: no route")
        return []
    if payload.get("code") != "0":
        log.warning("OKX returned non-zero: %s", payload)
        return []
    raw = payload.get("data") or []
    return candles_from_okx(raw)


def candles_from_okx(raw: list) -> List[Candle]:
    """
    Конвертирует сырой ответ OKX в список Candle.

    Формат OKX: [ts_ms, open, high, low, close, vol_base, vol_quote, _, _]
    """
    out: List[Candle] = []
    for row in raw:
        try:
            ts = int(row[0])
            o, h, l, c = (float(x) for x in row[1:5])
            v = float(row[5])  # base asset volume
            out.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v))
        except (ValueError, IndexError, TypeError):
            continue
    out.sort(key=lambda c: c.timestamp)
    return out


def fetch_multi_tf(
    inst_id: str,
    timeframes: List[str],
    limit: int = 300,
) -> dict:
    """
    Тянет несколько таймфреймов параллельно (последовательно, чтобы не нагружать API).
    Возвращает {tf: [Candle, ...]}.
    """
    out = {}
    for tf in timeframes:
        out[tf] = fetch_okx_candles(inst_id=inst_id, bar=tf, limit=limit)
    return out
