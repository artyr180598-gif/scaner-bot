"""
Утилита: загрузить OKX данные из локального JSON-файла и прогнать бэктест.

В песочнице Arena прямой HTTPS заблокирован, поэтому данные нужно
получить через fetch_page агентским инструментом и сохранить в файл
вручную (см. README секции BACKTEST).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..backtest.data_loader import candles_from_okx
from ..backtest.engine import BacktestEngine


def load_candles_from_okx_payload(payload: dict) -> list:
    return candles_from_okx(payload.get("data") or [])


def main(path: str = "backtest/cache/btc_1h_720.json") -> int:
    p = Path(path)
    if not p.exists():
        print(f"[ERR] {p} not found.")
        print("Получи данные через fetch_page и сохрани JSON. Пример:")
        print('  curl "https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1H&limit=720" > btc.json')
        return 1
    with p.open() as f:
        payload = json.load(f)
    candles = load_candles_from_okx_payload(payload)
    print(f"loaded {len(candles)} candles from {p}")
    if not candles:
        return 1

    engine = BacktestEngine(symbol="BTC-USDT", timeframe="1H", days=30, max_bars=720)
    result = engine.run(candles=candles, progress=True)
    print(result.report())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "backtest/cache/btc_1h_720.json"))
