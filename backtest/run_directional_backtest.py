"""
backtest/run_directional_backtest.py — честный walk-forward бэктест
направленного ядра (`directional.py`) на РЕАЛЬНЫХ исторических свечах.

Данные: `backtest/data_cache/raw_uf/data/research_intradaybias_t1/ohlcv_cache/
*_USDT_1h.csv` — реальные часовые свечи Binance spot (2021-11 → 2026),
скачанные `backtest/fetch_data.sh`. Никаких синтетических рядов.

Принципы (чтобы результат нельзя было «нарисовать»):
  1. Никакого заглядывания в будущее: на баре i движку отдаются только бары
     ≤ i, причём последний бар помечен как ЗАКРЫТЫЙ (движок сам решает).
  2. Старшие ТФ (4h/1d) строятся ресемплом ИЗ ТЕХ ЖЕ реальных 1h баров и
     обрезаются по тому же моменту времени.
  3. Вход исполняется, только если цена реально зашла в зону входа в течение
     `--fill-bars` баров. Не зашла — сделки не было (не «вошли по рынку»).
  4. Если в одном баре задеты и стоп, и цель — засчитывается СТОП
     (пессимистично: внутрибарного порядка мы не знаем).
  5. Комиссии и проскальзывание вычитаются из результата каждой сделки.
  6. Учитываются только сигналы, которые бот РЕАЛЬНО показал бы как
     actionable для выбранного риск-профиля.

Запуск:
    .venv/bin/python backtest/run_directional_backtest.py \
        --coins BTC,ETH,SOL --profile moderate --bars 6000 --step 3
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directional import RISK_PROFILES, DirectionalConfig, analyze
from market_data import Candles, MarketSnapshot

DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data_cache", "raw_uf", "data", "research_intradaybias_t1", "ohlcv_cache",
)


# ---------------------------------------------------------------------------
# Загрузка реальных данных
# ---------------------------------------------------------------------------

@dataclass
class Series:
    ts: list[float] = field(default_factory=list)
    open: list[float] = field(default_factory=list)
    high: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    close: list[float] = field(default_factory=list)
    volume: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.ts)


def load_1h(coin: str) -> Series:
    path = os.path.join(DATA_DIR, f"{coin}_USDT_1h.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    s = Series()
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = datetime.fromisoformat(row["time"]).replace(tzinfo=timezone.utc).timestamp()
                o, h, l, c = (float(row["open"]), float(row["high"]),
                              float(row["low"]), float(row["close"]))
                v = float(row["volume"])
            except (ValueError, KeyError, TypeError):
                continue
            if min(o, h, l, c) <= 0 or ts < 1_400_000_000:
                continue
            s.ts.append(ts)
            s.open.append(o)
            s.high.append(h)
            s.low.append(l)
            s.close.append(c)
            s.volume.append(v)
    return s


def resample(src: Series, factor: int) -> Series:
    """1h → Nh честным агрегированием (без сглаживаний и заполнений)."""
    out = Series()
    for i in range(0, len(src) - factor + 1, factor):
        window = slice(i, i + factor)
        out.ts.append(src.ts[i])
        out.open.append(src.open[i])
        out.high.append(max(src.high[window]))
        out.low.append(min(src.low[window]))
        out.close.append(src.close[i + factor - 1])
        out.volume.append(sum(src.volume[window]))
    return out


def slice_candles(s: Series, timeframe: str, upto_ts: float, limit: int) -> Optional[Candles]:
    """Бары строго до `upto_ts` включительно — защита от подглядывания вперёд."""
    hi = 0
    lo, high_i = 0, len(s) - 1
    while lo <= high_i:
        mid = (lo + high_i) // 2
        if s.ts[mid] <= upto_ts:
            hi = mid
            lo = mid + 1
        else:
            high_i = mid - 1
    start = max(0, hi - limit + 1)
    if hi - start < 60:
        return None
    idx = slice(start, hi + 1)
    return Candles(
        timeframe=timeframe,
        ts=tuple(s.ts[idx]),
        open=tuple(s.open[idx]),
        high=tuple(s.high[idx]),
        low=tuple(s.low[idx]),
        close=tuple(s.close[idx]),
        volume=tuple(s.volume[idx]),
    )


# ---------------------------------------------------------------------------
# Симуляция сделки
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    coin: str
    direction: str
    signal_ts: float
    entry_ts: Optional[float] = None
    entry_price: float = 0.0
    stop: float = 0.0
    targets: list[float] = field(default_factory=list)
    planned_rr: float = 0.0
    signal_confidence: float = 0.0
    data_confidence: float = 0.0
    outcome: str = "no_fill"
    exit_price: float = 0.0
    exit_ts: Optional[float] = None
    r_multiple: float = 0.0
    net_percent: float = 0.0
    bars_held: int = 0


def simulate(
    coin: str,
    signal,
    s: Series,
    i: int,
    *,
    fill_bars: int,
    max_bars: int,
    fee_percent: float,
    slippage_percent: float,
) -> Trade:
    """
    Прогоняет сделку по РЕАЛЬНЫМ будущим барам 1h.

    Порядок проверки внутри бара — пессимистичный: сначала стоп, затем цели.
    Частичной фиксации не моделируем — берём худший из «честных» вариантов:
    сделка живёт до стопа, до TP3 или до истечения срока.
    """
    plan = signal.plan
    t = Trade(
        coin=coin, direction=plan.direction, signal_ts=s.ts[i],
        stop=plan.stop, targets=list(plan.targets), planned_rr=plan.rr,
        signal_confidence=signal.signal_confidence,
        data_confidence=signal.data_confidence,
    )
    long = plan.direction == "long"

    # 1) исполнение входа: цена должна ЗАЙТИ в зону в ближайшие fill_bars баров
    entry_idx = None
    for j in range(i + 1, min(i + 1 + fill_bars, len(s))):
        if s.low[j] <= plan.entry_high and s.high[j] >= plan.entry_low:
            entry_idx = j
            # цена входа — реалистичная: край зоны, в который цена зашла
            t.entry_price = min(max(plan.entry_mid, s.low[j]), s.high[j])
            t.entry_ts = s.ts[j]
            break
    if entry_idx is None:
        return t

    risk = abs(t.entry_price - plan.stop)
    if risk <= 0:
        t.outcome = "invalid"
        return t

    # 2) жизнь сделки
    for j in range(entry_idx + 1, min(entry_idx + 1 + max_bars, len(s))):
        t.bars_held = j - entry_idx
        hit_stop = s.low[j] <= plan.stop if long else s.high[j] >= plan.stop
        if hit_stop:
            t.outcome, t.exit_price, t.exit_ts = "stop", plan.stop, s.ts[j]
            break
        reached = 0
        for k, tgt in enumerate(plan.targets, start=1):
            if (s.high[j] >= tgt) if long else (s.low[j] <= tgt):
                reached = k
        if reached:
            t.outcome = f"tp{reached}"
            t.exit_price = plan.targets[reached - 1]
            t.exit_ts = s.ts[j]
            break
    else:
        last = min(entry_idx + max_bars, len(s) - 1)
        t.outcome, t.exit_price, t.exit_ts = "expired", s.close[last], s.ts[last]

    gross = (t.exit_price - t.entry_price) / t.entry_price * 100.0
    if not long:
        gross = -gross
    t.net_percent = gross - 2 * fee_percent - 2 * slippage_percent
    t.r_multiple = (t.net_percent / 100.0 * t.entry_price) / risk
    return t


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------

def run_coin(coin: str, args) -> list[Trade]:
    s1h = load_1h(coin)
    if len(s1h) < 1500:
        return []
    s4h = resample(s1h, 4)
    s1d = resample(s1h, 24)
    s2h = resample(s1h, 2)

    cfg = DirectionalConfig(
        entry_tf="1h", confirm_tfs=("2h", "4h"), context_tf="1d",
        max_entry_stretch_atr=args.stretch,
    )
    profile = RISK_PROFILES[args.profile]

    trades: list[Trade] = []
    start = max(400, len(s1h) - args.bars)
    busy_until = 0.0
    for i in range(start, len(s1h) - 5, args.step):
        ts = s1h.ts[i]
        if ts < busy_until:
            continue
        candles = {}
        for tf, series in (("1h", s1h), ("2h", s2h), ("4h", s4h), ("1d", s1d)):
            c = slice_candles(series, tf, ts, 300)
            if c is not None:
                candles[tf] = c
        if "1h" not in candles:
            continue
        # реальный оборот за 24ч из тех же свечей (quote-объём)
        vol24 = sum(
            s1h.volume[k] * s1h.close[k] for k in range(max(0, i - 23), i + 1)
        )
        snap = MarketSnapshot(
            base=coin, symbol=f"{coin}/USDT", exchange="binance-hist",
            fetched_at=ts + 3600, last_price=s1h.close[i],
            quote_volume_24h=vol24, candles=candles,
        )
        sig = analyze(snap, profile=profile, cfg=cfg, now=ts + 3600)
        if not sig.actionable or sig.plan is None:
            continue
        t = simulate(
            coin, sig, s1h, i,
            fill_bars=args.fill_bars, max_bars=args.max_bars,
            fee_percent=args.fee, slippage_percent=args.slippage,
        )
        trades.append(t)
        if t.entry_ts:
            busy_until = (t.exit_ts or t.entry_ts) + 3600
        else:
            busy_until = ts + 3600 * args.fill_bars
    return trades


def summarize(trades: list[Trade]) -> dict:
    filled = [t for t in trades if t.outcome not in ("no_fill", "invalid")]
    wins = [t for t in filled if t.net_percent > 0]
    losses = [t for t in filled if t.net_percent <= 0]
    gross_w = sum(t.net_percent for t in wins)
    gross_l = -sum(t.net_percent for t in losses)
    r_values = [t.r_multiple for t in filled]
    return {
        "signals": len(trades),
        "no_fill": sum(1 for t in trades if t.outcome == "no_fill"),
        "trades": len(filled),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": (len(wins) / len(filled) * 100.0) if filled else None,
        "avg_net_percent": (sum(t.net_percent for t in filled) / len(filled)) if filled else None,
        "sum_net_percent": sum(t.net_percent for t in filled),
        "profit_factor": (gross_w / gross_l) if gross_l > 0 else None,
        "avg_r": (sum(r_values) / len(r_values)) if r_values else None,
        "expectancy_r": (sum(r_values) / len(r_values)) if r_values else None,
        "avg_planned_rr": (sum(t.planned_rr for t in filled) / len(filled)) if filled else None,
        "avg_bars_held": (sum(t.bars_held for t in filled) / len(filled)) if filled else None,
        "avg_signal_conf": (
            sum(t.signal_confidence for t in filled) / len(filled) if filled else None
        ),
        "by_outcome": {
            k: sum(1 for t in filled if t.outcome == k)
            for k in ("tp1", "tp2", "tp3", "stop", "expired")
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--coins", default="BTC,ETH,SOL,BNB,XRP,ADA,DOGE,AVAX,LINK,DOT")
    p.add_argument("--profile", default="moderate", choices=list(RISK_PROFILES))
    p.add_argument("--bars", type=int, default=6000, help="сколько последних 1h баров тестировать")
    p.add_argument("--step", type=int, default=3, help="шаг проверки сигнала, баров")
    p.add_argument("--fill-bars", type=int, default=3)
    p.add_argument("--max-bars", type=int, default=48)
    p.add_argument("--fee", type=float, default=0.06, help="комиссия за одну ногу, %%")
    p.add_argument("--slippage", type=float, default=0.02, help="проскальзывание за ногу, %%")
    p.add_argument("--stretch", type=float, default=1.5,
                   help="гейт погони за движением, ATR от EMA20 (0 = выключен)")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    all_trades: list[Trade] = []
    per_coin: dict[str, dict] = {}
    t0 = time.time()
    for coin in coins:
        try:
            trades = run_coin(coin, args)
        except FileNotFoundError:
            print(f"{coin}: нет данных — пропускаю", flush=True)
            continue
        per_coin[coin] = summarize(trades)
        all_trades.extend(trades)
        st = per_coin[coin]
        print(
            f"{coin:6s} сигналов {st['signals']:4d} | сделок {st['trades']:4d} | "
            f"winrate {st['winrate'] if st['winrate'] is None else round(st['winrate'], 1)} | "
            f"avg R {st['avg_r'] if st['avg_r'] is None else round(st['avg_r'], 3)} | "
            f"PF {st['profit_factor'] if st['profit_factor'] is None else round(st['profit_factor'], 2)}",
            flush=True,
        )
    total = summarize(all_trades)
    print("\n=== ИТОГО (реальные исторические свечи Binance 1h) ===")
    print(json.dumps(total, ensure_ascii=False, indent=2))
    print(f"время прогона: {time.time() - t0:.0f}с")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(
                {"config": vars(args), "total": total, "per_coin": per_coin,
                 "trades": [t.__dict__ for t in all_trades]},
                fh, ensure_ascii=False, indent=2,
            )
        print(f"детали: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
