"""
prepare_data.py — сборка компактного кеша исторических данных для бектеста.

Источник — реальный публичный датасет (Binance spot + Binance USDT-M perp +
funding 8ч), зеркалируемый в GitHub-репозитории brasdor/UngerFink-TREND
(данные обновляются автором репозитория автоматически, покрытие 2019→сейчас).

Шаги:
  1. fetch_data.sh (или руками, см. AI_AGENTS/DATA_SOURCES.md) качает сырые CSV
     в backtest/data_cache/raw_uf/ (sparse-clone, ~600 МБ, НЕ в git).
  2. Этот скрипт нормализует их в единый формат
     backtest/data_cache/dataset/{spot_1h,spot_1d,perp_1d,perp_4h,funding}/{SYM}.csv.gz
     Схема: ts (мс UTC), open, high, low, close, volume  |  funding: ts, rate_pct.
  3. Сырьё можно удалить (--prune-raw) — кеш компактный (~40 МБ).

Запуск:  .venv/bin/python backtest/prepare_data.py [--prune-raw]
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from pathlib import Path

import pandas as pd

RAW = Path(__file__).resolve().parent / "data_cache" / "raw_uf"
OUT = Path(__file__).resolve().parent / "data_cache" / "dataset"

SPOT_1H_DIR = RAW / "data" / "research_intradaybias_t1" / "ohlcv_cache"
SPOT_1D_DIR = RAW / "data" / "universe" / "ohlcv_1d"
PERP_1D_DIR = RAW / "data" / "futures_universe" / "ohlcv_1d"
PERP_4H_DIR = RAW / "data" / "futures_universe" / "ohlcv_4h"
FUND_DIR = RAW / "data" / "futures_universe" / "funding_rates"

MIN_TS = 1483228800000  # 2017-01-01: отсекаем битые строки (1970-й и пр.)


def _read_iso(path: Path) -> pd.DataFrame:
    """CSV вида time,open,high,low,close,volume с ISO-временем (данные spot)."""
    df = pd.read_csv(path)
    # pandas 3.x возвращает datetime64[us] — явно приводим к ns перед int64
    dt = pd.to_datetime(df["time"], utc=True, errors="coerce")
    ok = dt.notna()
    df = df[ok].copy()
    df["ts"] = dt[ok].dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]").astype("int64") // 10**6
    df = df[["ts", "open", "high", "low", "close", "volume"]]
    return _clean(df)


def _read_ms(path: Path) -> pd.DataFrame:
    """CSV вида timestamp,open,high,low,close,volume[,date] с мс-временем (данные perp)."""
    df = pd.read_csv(path)
    df = df.rename(columns={"timestamp": "ts"})
    df = df[["ts", "open", "high", "low", "close", "volume"]]
    return _clean(df)


def _read_funding(path: Path) -> pd.DataFrame:
    """CSV funding_time,funding_rate → ts, rate_pct (доля за 8ч → % за 8ч)."""
    df = pd.read_csv(path)
    df = df.rename(columns={"funding_time": "ts", "funding_rate": "rate"})
    df["rate_pct"] = df["rate"].astype(float) * 100.0
    df = df[["ts", "rate_pct"]]
    df = df.dropna()
    df = df[(df["ts"] >= MIN_TS) & (df["rate_pct"].abs() < 10.0)]  # >10%/8ч — мусор
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna()
    df = df[(df["ts"] >= MIN_TS) & (df["close"] > 0) & (df["open"] > 0)]
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    return df


def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as fh:
        df.to_csv(fh, index=False, float_format="%.10g")


def main() -> int:
    if not RAW.exists():
        print("Нет сырых данных. Сначала запусти backtest/fetch_data.sh "
              "(см. AI_AGENTS/DATA_SOURCES.md).")
        return 1

    # символы с полным набором: spot 1h + spot 1d + perp 1d + perp 4h + funding
    spot_1h = {p.name.removesuffix("_USDT_1h.csv") for p in SPOT_1H_DIR.glob("*_USDT_1h.csv")}
    spot_1d = {p.name.removesuffix("_USDT_1d.csv") for p in SPOT_1D_DIR.glob("*_USDT_1d.csv")}
    perp_1d = {p.name.removesuffix("USDT_1d.csv") for p in PERP_1D_DIR.glob("*USDT_1d.csv")}
    perp_4h = {p.name.removesuffix("USDT_4h.csv") for p in PERP_4H_DIR.glob("*USDT_4h.csv")}
    fund = {p.name.removesuffix("USDT_funding.csv") for p in FUND_DIR.glob("*USDT_funding.csv")}
    universe = sorted(spot_1h & spot_1d & perp_1d & perp_4h & fund)
    print(f"Символы с полным набором данных: {len(universe)}")

    total_rows = 0
    for i, sym in enumerate(universe, 1):
        frames = {
            "spot_1h": _read_iso(SPOT_1H_DIR / f"{sym}_USDT_1h.csv"),
            "spot_1d": _read_iso(SPOT_1D_DIR / f"{sym}_USDT_1d.csv"),
            "perp_1d": _read_ms(PERP_1D_DIR / f"{sym}USDT_1d.csv"),
            "perp_4h": _read_ms(PERP_4H_DIR / f"{sym}USDT_4h.csv"),
            "funding": _read_funding(FUND_DIR / f"{sym}USDT_funding.csv"),
        }
        for kind, df in frames.items():
            _write(df, OUT / kind / f"{sym}.csv.gz")
            total_rows += len(df)
        if i % 20 == 0 or i == len(universe):
            print(f"  {i}/{len(universe)} обработано")

    print(f"Готово: {len(universe)} символов, {total_rows:,} строк → {OUT}")
    if "--prune-raw" in sys.argv:
        shutil.rmtree(RAW, ignore_errors=True)
        print("Сырые данные удалены (кеш компактный; при необходимости — fetch_data.sh).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
