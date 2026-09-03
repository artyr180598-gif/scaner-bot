"""Real-data self-test.

Runs against live Binance/Bybit endpoints only. There is no synthetic market
data in this module — if the exchange is unavailable, the check fails loudly.
Run with: python -m cryptoforge_pro.selftest
"""

from __future__ import annotations

import asyncio

from loguru import logger

from cryptoforge_pro.analysis.engine import SignalEngine
from cryptoforge_pro.config import get_settings
from cryptoforge_pro.data.http import HttpSession
from cryptoforge_pro.market import MarketService
from cryptoforge_pro.telegram.format import format_signal
from cryptoforge_pro.utils import configure_logging


async def _self_test() -> None:
    settings = get_settings()
    http = HttpSession(timeout=settings.http_timeout)
    await http.start()
    try:
        from cryptoforge_pro.data.coinglass import CoinglassProvider
        from cryptoforge_pro.data.exchanges import build_exchange_router
        from cryptoforge_pro.data.news import NewsProvider

        exchange = build_exchange_router(http, settings.exchange_list)
        coinglass = CoinglassProvider(http, settings.coinglass_api_key, settings.coinglass_base_url)
        news = NewsProvider(http, settings.cryptopanic_api_key, settings.news_language)
        market = MarketService(settings, exchange, coinglass, news)

        logger.info("1/3 Loading ticker universe...")
        tickers = await market.top_tickers(count=30)
        if not tickers:
            raise RuntimeError("Binance/Bybit returned no USDT tickers")
        logger.info("   got {} tickers, first: {}", len(tickers), tickers[0].symbol)

        selected = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        for symbol in selected:
            if any(t.symbol == symbol for t in tickers):
                selected = [symbol]
                break
        logger.info("2/3 Loading real candles / market data for {}...", selected[0])
        data = await market.get_market_data(
            selected[0],
            timeframes=["1h", "4h"],
            include_derivatives=True,
            include_news=True,
        )
        if not data.candles:
            raise RuntimeError("No candles returned")
        logger.info("   price={} candles={} funding={}", data.price, {k: len(v) for k, v in data.candles.items()}, data.derivatives.funding_rate)

        engine = SignalEngine(settings)
        signal = engine.build_signal(data, mode="best", risk_profile="balanced")
        logger.info("3/3 Signal engine finished (signal={})", signal.label if signal else "none above threshold")
        if signal:
            print(format_signal(signal))
            print("\n✅ Real-data self-test passed.")
        else:
            print("✅ Real-data self-test passed (no qualifying setup at this moment).")
    finally:
        await http.close()


def main() -> None:
    configure_logging("INFO")
    try:
        asyncio.run(_self_test())
    except Exception as exc:  # noqa: BLE001
        logger.error("Self-test FAILED: {}", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
