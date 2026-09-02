"""
research/data.py — загрузка реальных часовых свечей в панель pandas.

Источник тот же, что у боевого бэктеста: backtest/data_cache/raw_uf/...
(реальные Binance spot 1h). Никаких синтетических данных.
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "backtest", "data_cache", "raw_uf", "data",
                        "research_intradaybias_t1", "ohlcv_cache")
CACHE = os.path.join(ROOT, "research", "cache")
os.makedirs(CACHE, exist_ok=True)


def available_coins() -> list[str]:
    out = []
    for name in os.listdir(DATA_DIR):
        if name.endswith("_USDT_1h.csv"):
            out.append(name[: -len("_USDT_1h.csv")])
    return sorted(out)


def load_coin(coin: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, f"{coin}_USDT_1h.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["time"])
    df = df.rename(columns={"time": "ts"})
    df = df.dropna()
    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]
    if len(df) < 2000:
        return None
    df = df.sort_values("ts").drop_duplicates("ts")
    df["coin"] = coin
    return df.reset_index(drop=True)


def pick_liquid_coins(n: int, min_bars: int = 12000) -> list[str]:
    """Отбор монет по медианному долларовому обороту 1h — только реальные данные."""
    cache = os.path.join(CACHE, f"liquid_{n}_{min_bars}.txt")
    if os.path.exists(cache):
        return open(cache).read().split()
    rows = []
    for coin in available_coins():
        path = os.path.join(DATA_DIR, f"{coin}_USDT_1h.csv")
        try:
            df = pd.read_csv(path, usecols=["close", "volume"])
        except Exception:
            continue
        if len(df) < min_bars:
            continue
        dv = (df["close"] * df["volume"]).median()
        rows.append((coin, float(dv), len(df)))
    rows.sort(key=lambda r: -r[1])
    picked = [r[0] for r in rows[:n]]
    with open(cache, "w") as fh:
        fh.write("\n".join(picked))
    return picked


def load_panel(coins: list[str]) -> pd.DataFrame:
    frames = []
    for c in coins:
        df = load_coin(c)
        if df is not None:
            frames.append(df)
    panel = pd.concat(frames, ignore_index=True)
    return panel.sort_values(["coin", "ts"]).reset_index(drop=True)


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    coins = pick_liquid_coins(n)
    print(f"{len(coins)} монет:", " ".join(coins))
