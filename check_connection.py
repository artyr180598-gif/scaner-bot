"""
check_connection.py — проверка реального доступа к биржам (диагностика).

Опрашивает ПУБЛИЧНЫЕ REST-эндпоинты бирж через ccxt — ровно тот же
конвейер, что использует сканер, — и печатает реальные лучшие цены
стакана (bid/ask) для BTC/USDT на споте и фьючерсах.

Зачем нужно:
  * убедиться, что с вашего IP/региона биржи отдают данные без API-ключей;
  * сразу увидеть гео-блокировки (например, Binance блокирует IP США,
    где по умолчанию хостится Railway).

Запуск:
    python check_connection.py                # все биржи из EXCHANGES/.env
    python check_connection.py mexc bybit     # только выбранные
    python check_connection.py binance gate okx
"""

from __future__ import annotations

import asyncio
import sys
import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

import ccxt.async_support as ccxt

from config import SUPPORTED_EXCHANGES
from scanner import EXCHANGE_REGISTRY

PROBE_SYMBOL_SPOT = "BTC/USDT"
PROBE_SYMBOL_FUTURES = "BTC/USDT:USDT"  # линейный перпетуал USDT-M


async def probe_side(exchange_id: str, market_type: str) -> dict:
    """Опрашивает одну сторону биржи и возвращает результат проверки."""
    spec = EXCHANGE_REGISTRY[exchange_id]
    class_name = spec.spot_class if market_type == "spot" else spec.futures_class
    default_type = spec.spot_default_type if market_type == "spot" else spec.futures_default_type
    symbol = PROBE_SYMBOL_SPOT if market_type == "spot" else PROBE_SYMBOL_FUTURES

    exchange = getattr(ccxt, class_name)({
        "enableRateLimit": True,
        "timeout": 15_000,
        "aiohttp_trust_env": True,
        "options": {"defaultType": default_type},
    })
    started = time.monotonic()
    try:
        book = await exchange.fetch_order_book(symbol, 5)
        bid_price, bid_qty = book["bids"][0]
        ask_price, ask_qty = book["asks"][0]
        return {
            "ok": True,
            "exchange": exchange_id,
            "market": market_type,
            "symbol": symbol,
            "bid": float(bid_price),
            "bid_qty": float(bid_qty),
            "ask": float(ask_price),
            "ask_qty": float(ask_qty),
            "latency_ms": (time.monotonic() - started) * 1000,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — любая ошибка = недоступность
        return {
            "ok": False,
            "exchange": exchange_id,
            "market": market_type,
            "symbol": symbol,
            "bid": None, "bid_qty": None, "ask": None, "ask_qty": None,
            "latency_ms": (time.monotonic() - started) * 1000,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        await exchange.close()


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:.8f}"


async def main(argv: list[str]) -> int:
    if argv:
        exchange_ids = [a.strip().lower() for a in argv if a.strip()]
    else:
        import os
        env_exchanges = os.getenv("EXCHANGES", "")
        exchange_ids = (
            [e.strip().lower() for e in env_exchanges.split(",") if e.strip()]
            or list(SUPPORTED_EXCHANGES)
        )

    unknown = [e for e in exchange_ids if e not in SUPPORTED_EXCHANGES]
    if unknown:
        print(f"Неизвестные биржи {unknown}. Доступно: {', '.join(SUPPORTED_EXCHANGES)}")
        return 2

    print(f"Проверяю публичный доступ к биржам: {', '.join(exchange_ids)}\n")

    tasks = [
        probe_side(exchange_id, market_type)
        for exchange_id in exchange_ids
        for market_type in ("spot", "futures")
    ]
    results = await asyncio.gather(*tasks)

    ok_count = 0
    for r in results:
        header = f"{r['exchange'].upper():10s} {r['market']:8s} {r['symbol']:15s}"
        if r["ok"]:
            ok_count += 1
            print(
                f"{header} ✅  bid {fmt_price(r['bid']):>12s} × {fmt_price(r['bid_qty']):>10s}   "
                f"ask {fmt_price(r['ask']):>12s} × {fmt_price(r['ask_qty']):>10s}   "
                f"({r['latency_ms']:.0f} ms)"
            )
        else:
            print(f"{header} ❌  {r['error'][:120]}")

    total = len(results)
    print(f"\nИтого: {ok_count}/{total} источников данных доступны.")
    if ok_count == 0:
        print("Ни одна биржа не отвечает: проверьте сеть/регион хостинга или задайте HTTPS_PROXY.")
        return 1
    if ok_count < total:
        print(
            "Подсказка: часть бирж гео-блокируют датацентры (Binance — IP США, "
            "Bybit/OKX — в отдельных регионах). Сканер просто пропустит недоступные "
            "биржи и продолжит работать с остальными; полностью доступ можно вернуть, "
            "сменив регион Railway или задав переменную HTTPS_PROXY."
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
