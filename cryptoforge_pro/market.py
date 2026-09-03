"""Market aggregator that combines exchange, derivatives, news and BTC context."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from cryptoforge_pro.config import Settings
from cryptoforge_pro.data.coinglass import CoinglassProvider
from cryptoforge_pro.data.exchanges import ExchangeRouter, MarketDataUnavailable
from cryptoforge_pro.data.news import NewsProvider
from cryptoforge_pro.data.sentiment import FearGreedProvider
from cryptoforge_pro.models import Candle, Derivatives, MarketData, TickerInfo
from cryptoforge_pro.utils import TTLCache

MIN_CANDLES = 80


class MarketService:
    def __init__(
        self,
        settings: Settings,
        exchanges: ExchangeRouter,
        coinglass: CoinglassProvider,
        news: NewsProvider,
        fear_greed: FearGreedProvider | None = None,
    ) -> None:
        self.settings = settings
        self.exchanges = exchanges
        self.coinglass = coinglass
        self.news = news
        self.fear_greed = fear_greed
        self.cache = TTLCache(ttl=settings.cache_ttl_seconds, max_size=1024)

    async def top_tickers(self, count: int = 150) -> list[TickerInfo]:
        key = "top_tickers"
        cached = await self.cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        tickers = await self.exchanges.get_tickers_24h()
        # Always include BTC for correlation; filter stable coins and low volume
        filtered = [
            t
            for t in tickers
            if t.base not in {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD"}
            and t.volume_24h_quote >= self.settings.min_volume_usd_24h
        ]
        filtered.sort(key=lambda t: t.volume_24h_quote, reverse=True)
        result = filtered[:count]
        await self.cache.set(key, result)
        return result

    async def get_price(self, symbol: str) -> float:
        """Fast single-symbol price from real exchange ticker data."""
        symbol = symbol.upper().strip()
        if symbol.endswith("USDT"):
            pass
        elif symbol.endswith("USDC"):
            symbol = symbol[:-4] + "USDT"
        elif symbol.endswith("USD"):
            symbol = symbol[:-3] + "USDT"
        else:
            symbol = symbol + "USDT"
        ticker = await self.exchanges.get_ticker(symbol)
        if ticker.last_price <= 0:
            raise MarketDataUnavailable(f"No real price for {symbol}")
        return ticker.last_price

    async def news_headlines(self, symbol: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        return await self.news.headlines(symbol, limit=limit)

    async def market_overview(self) -> dict[str, Any]:
        """Live market snapshot for the main dashboard."""
        tickers = await self.top_tickers(self.settings.top_n_symbols)
        if not tickers:
            raise MarketDataUnavailable("No live tickers for market overview")

        by_symbol = {t.symbol: t for t in tickers}
        btc, eth = by_symbol.get("BTCUSDT"), by_symbol.get("ETHUSDT")
        active = [t for t in tickers if t.volume_24h_quote >= max(self.settings.min_volume_usd_24h, 1_000_000)]
        gainers = sorted(active, key=lambda t: t.change_24h_pct, reverse=True)[:5]
        losers = sorted(active, key=lambda t: t.change_24h_pct)[:5]
        up = sum(1 for t in active if t.change_24h_pct > 0)
        down = len(active) - up
        avg_change = sum(t.change_24h_pct for t in active) / len(active) if active else 0.0
        total_volume = sum(t.volume_24h_quote for t in active)

        fng = await self.fear_greed.index() if self.fear_greed else None

        return {
            "btc": btc,
            "eth": eth,
            "gainers": gainers,
            "losers": losers,
            "up": up,
            "down": down,
            "avg_change": avg_change,
            "total_volume": total_volume,
            "active_count": len(active),
            "fng": fng,
        }

    async def data_status(self) -> dict[str, Any]:
        """Reachability check for every real-data provider used by the bot."""
        out: dict[str, Any] = {"sources": {}}
        sources = out["sources"]

        try:
            sources["exchanges"] = await self.exchanges.status()
        except Exception as exc:  # noqa: BLE001
            sources["exchanges"] = {"binance": {"ok": False, "detail": str(exc)}, "bybit": {"ok": False, "detail": str(exc)}}

        if self.coinglass.enabled:
            try:
                der = await self.coinglass.derivatives_summary("BTCUSDT")
                sources["coinglass"] = {
                    "ok": der.available,
                    "detail": "CoinGlass funding/OI test OK" if der.available else "CoinGlass endpoint responded but no derivatives payload",
                }
            except Exception as exc:  # noqa: BLE001
                sources["coinglass"] = {"ok": False, "detail": str(exc)}
        else:
            sources["coinglass"] = {"ok": False, "detail": "COINGLASS_API_KEY not configured — optional"}

        if self.news.enabled:
            try:
                items = await self.news.headlines(limit=1)
                sources["cryptopanic"] = {
                    "ok": bool(items),
                    "detail": "CryptoPanic headlines OK" if items else "CryptoPanic responded but returned no items",
                }
            except Exception as exc:  # noqa: BLE001
                sources["cryptopanic"] = {"ok": False, "detail": str(exc)}
        else:
            sources["cryptopanic"] = {"ok": False, "detail": "CRYPTOPANIC_API_KEY not configured — optional"}

        if self.fear_greed:
            try:
                fng = await self.fear_greed.index()
                sources["fear_greed"] = {
                    "ok": bool(fng),
                    "detail": f"Fear & Greed value={fng.get('value')}" if fng else "external.me unavailable",
                }
            except Exception as exc:  # noqa: BLE001
                sources["fear_greed"] = {"ok": False, "detail": str(exc)}

        ex_ok = [v.get("ok", False) for v in sources.get("exchanges", {}).values()]
        out["all_exchanges_ok"] = bool(ex_ok) and all(ex_ok)
        out["any_exchange_ok"] = bool(ex_ok) and any(ex_ok)
        return out

    async def get_market_data(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
        include_derivatives: bool = True,
        include_news: bool = True,
        include_btc_context: bool = True,
    ) -> MarketData:
        symbol = symbol.upper().strip()
        if symbol.endswith("USDT"):
            pass
        elif symbol.endswith("USDC"):
            symbol = symbol[:-4] + "USDT"
        elif symbol.endswith("USD"):
            symbol = symbol[:-3] + "USDT"
        else:
            symbol = symbol + "USDT"
        sfx: str = symbol.replace("/", "_")

        candles: dict[str, list[Candle]] = {}
        for tf in timeframes or self.settings.timeframe_list:
            cached = await self.cache.get(f"candles:{sfx}:{tf}")
            if cached is not None:
                candles[tf] = cached  # type: ignore[assignment]
                continue
            try:
                data = await self.exchanges.get_klines(symbol, tf, self.settings.candles_limit)
                if len(data) >= MIN_CANDLES:
                    candles[tf] = data
                    await self.cache.set(f"candles:{sfx}:{tf}", data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Candles {} {} unavailable: {}", symbol, tf, exc)

        if not candles:
            # try only the base timeframe, then hard fail — never fabricate
            raise MarketDataUnavailable(
                f"No real candles received for {symbol}. Check symbol name / network and try again."
            )

        try:
            ticker = await self.exchanges.get_ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            # Candles are still real (from the same exchange). We never invent a
            # price — use the close of the real last candle instead.
            logger.warning("Ticker {} unavailable: {}", symbol, exc)
            primary = next(iter(candles.values()))
            fallback_price = primary[-1].close if primary else 0.0
            ticker = TickerInfo(
                symbol=symbol,
                base=symbol.replace("USDT", ""),
                quote="USDT",
                last_price=fallback_price,
                source="candles",
            )

        derivatives: Derivatives = Derivatives(source="exchange")
        if include_derivatives:
            der_tasks = [self.exchanges.get_derivative(symbol)]
            if self.coinglass.enabled:
                der_tasks.append(self.coinglass.derivatives_summary(symbol))
            der_results = await asyncio.gather(*der_tasks, return_exceptions=True)
            for res in der_results:
                if isinstance(res, Derivatives) and res.available:
                    derivatives = res
                    break

        news: list[dict[str, Any]] = []
        if include_news and self.news.enabled:
            news = await self.news.headlines(symbol, limit=4)

        btc_change, corr = (0.0, 0.0)
        if include_btc_context:
            btc_change, corr = await self.btc_context(candles)

        data = MarketData(
            symbol=symbol,
            base=symbol.replace("USDT", ""),
            quote="USDT",
            price=ticker.last_price,
            candles=candles,
            ticker=ticker,
            derivatives=derivatives,
            news=news,
            btc_change_24h_pct=btc_change,
            btc_corr_returns=corr,
        )
        await self.cache.set(f"market:{sfx}", data)
        return data

    async def btc_context(self, candles: dict[str, list[Candle]]) -> tuple[float, float]:
        try:
            btc = await self.get_market_data(
                "BTCUSDT",
                timeframes=["1h"],
                include_derivatives=False,
                include_news=False,
                include_btc_context=False,
            )
            btc_hour = (btc.candles.get("1h") or [])
            btc_change = 0.0
            if len(btc_hour) > 1:
                btc_change = (btc_hour[-1].close - btc_hour[-2].close) / btc_hour[-1].open * 100.0
            corr = self._correlation(candles.get("1h", []), btc_hour)
            return btc_change, corr
        except Exception as exc:  # noqa: BLE001
            logger.debug("BTC context unavailable: {}", exc)
            return 0.0, 0.0

    @staticmethod
    def _correlation(a: list[Candle], b: list[Candle]) -> float:
        if len(a) < 10 or len(b) < 10:
            return 0.0
        ra = [a[i].close / a[i - 1].close for i in range(1, len(a))]
        rb = [b[i].close / b[i - 1].close for i in range(1, len(b))]
        n = min(len(ra), len(rb))
        if n < 10:
            return 0.0
        ra, rb = ra[-n:], rb[-n:]
        ma = sum(ra) / n
        mb = sum(rb) / n
        var_a = sum((x - ma) ** 2 for x in ra)
        var_b = sum((x - mb) ** 2 for x in rb)
        if var_a == 0 or var_b == 0:
            return 0.0
        cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
        return cov / ((var_a * var_b) ** 0.5)

    def universe(self, tickers: list[TickerInfo]) -> list[str]:
        return sorted({t.symbol for t in tickers})
