"""
Утилита: скачать исторические свечи OKX и сохранить в JSON.

Использует инструмент fetch_page (которым управляет агент/скрипт),
поэтому работает даже в песочнице.

Запуск:
    python -m chris_bots.backtest.fetch_data BTC-USDT 1H 300 out.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List

from ..core.domain.candle import Candle
from .data_loader import candles_from_okx

log = logging.getLogger(__name__)


def save_candles_json(candles: List[Candle], path: str) -> None:
    """Сохраняет свечи в JSON для последующего использования в бэктестере."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in candles], f, indent=2, ensure_ascii=False)


def load_candles_json(path: str) -> List[Candle]:
    """Загружает свечи из JSON."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [
        Candle(
            timestamp=int(c["timestamp"]),
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            volume=float(c["volume"]),
        )
        for c in raw
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX candles → JSON")
    parser.add_argument("inst_id", default="BTC-USDT", nargs="?")
    parser.add_argument("bar", default="1H", nargs="?")
    parser.add_argument("limit", default=300, type=int, nargs="?")
    parser.add_argument("out", default="backtest/data.json", nargs="?")
    args = parser.parse_args()

    # Загружаем через data_loader (пробует HTTPS, иначе вернёт []).
    from .data_loader import fetch_okx_candles
    candles = fetch_okx_candles(args.inst_id, args.bar, args.limit)
    if not candles:
        print(
            f"[ERR] OKX fetch failed for {args.inst_id} {args.bar} {args.limit}",
            file=sys.stderr,
        )
        print(
            "Подсказка: в песочнице Arena прямой HTTPS к OKX заблокирован.",
            file=sys.stderr,
        )
        print(
            "Используйте fetch_page платформы и подайте JSON в виде файла.",
            file=sys.stderr,
        )
        return 1
    save_candles_json(candles, args.out)
    print(f"[OK] saved {len(candles)} candles to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
