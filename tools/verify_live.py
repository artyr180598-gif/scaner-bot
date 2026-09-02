"""
tools/verify_live.py — прогон направленного движка на РЕАЛЬНОМ live-срезе биржи.

Зачем отдельный скрипт: сеть песочницы Arena не пускает к биржевым API из
bash (см. AI_AGENTS/DATA_SOURCES.md), поэтому live-данные снимаются
платформенным инструментом fetch_page и складываются в
`backtest/live_snapshots/*.json` КАК ЕСТЬ (цены/объёмы/время — без правок).
Скрипт читает такой файл и гоняет ровно тот же `directional.analyze()`,
который работает в боте.

Использование:
    .venv/bin/python tools/verify_live.py backtest/live_snapshots/okx_btc_2026-09-02.json
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from directional import RISK_PROFILES, DirectionalConfig, analyze, beginner_breakdown
from market_data import Candles, MarketSnapshot, OrderBookTop


def load_snapshot(path: str) -> tuple[MarketSnapshot, float, DirectionalConfig]:
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    ticker = blob["ticker"]
    candles: dict[str, Candles] = {}
    newest_ts = 0.0
    for tf, payload in blob["series"].items():
        step = int(payload["step_ms"])
        last = int(payload["last_ts_ms"])
        rows = []
        for i, line in enumerate(payload["rows"]):
            o, h, l, c, v = (float(x) for x in line.split(","))
            rows.append([last - i * step, o, h, l, c, v])
        candles[tf] = Candles.from_rows(tf, rows)
        newest_ts = max(newest_ts, (last + step) / 1000.0)

    snap = MarketSnapshot(
        base=ticker["instId"].split("-")[0],
        symbol=ticker["instId"],
        exchange="okx",
        fetched_at=float(ticker["ts"]) / 1000.0,
        last_price=float(ticker["last"]),
        quote_volume_24h=float(ticker["volCcy24h"]),
        price_change_24h_percent=(
            (float(ticker["last"]) / float(ticker["open24h"]) - 1.0) * 100.0
        ),
        candles=candles,
        book=OrderBookTop(bid=float(ticker["bidPx"]), ask=float(ticker["askPx"])),
    )
    tfs = sorted(candles, key=lambda t: len(t))
    cfg = DirectionalConfig(
        entry_tf="15m", confirm_tfs=("1h",), context_tf="4h",
        min_bars=50, full_bars=60,   # live-срез снят по 60 баров на ТФ
    )
    return snap, float(ticker["ts"]) / 1000.0, cfg


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else (
        "backtest/live_snapshots/okx_btc_2026-09-02.json"
    )
    snap, now, cfg = load_snapshot(path)
    print(f"Источник: {path}")
    print(f"Монета: {snap.base} на {snap.exchange}, цена {snap.last_price}")
    print(f"Оборот 24ч: ${snap.quote_volume_24h:,.0f}, спред стакана "
          f"{snap.book.spread_percent:.4f}%")
    print(f"Свечей: " + ", ".join(f"{tf}={len(c)}" for tf, c in snap.candles.items()))
    print()

    for key in ("conservative", "moderate", "aggressive"):
        sig = analyze(snap, profile=key, cfg=cfg, now=now)
        print("=" * 78)
        print(f"ПРОФИЛЬ {RISK_PROFILES[key].title}")
        print(f"  направление       : {sig.direction}  (actionable={sig.actionable})")
        print(f"  уверенность данных: {sig.data_confidence:.1f}%")
        print(f"  уверенность прогн.: {sig.signal_confidence:.1f}%")
        print(f"  net score         : {sig.net_score:+.3f}, согласие групп "
              f"{sig.agreement * 100:.0f}%, согласных групп {sig.agreeing_groups}")
        print(f"  группы            : " + ", ".join(
            f"{k}={v:+.2f}" for k, v in sorted(sig.group_scores.items())))
        if sig.plan:
            p = sig.plan
            print(f"  вход              : {p.entry_low:.1f} – {p.entry_high:.1f}")
            print(f"  стоп              : {p.stop:.1f} ({p.risk_percent_of_price:.2f}%)")
            print(f"  цели              : " + ", ".join(
                f"TP{i}={t:.1f} (R/R {rr:.2f})"
                for i, (t, rr) in enumerate(zip(p.targets, p.target_rr), 1)))
            print(f"  плечо             : до {p.max_leverage:g}×, позиция "
                  f"{p.position_percent_of_deposit:.0f}% депозита")
            print(f"  горизонт          : {p.horizon}")
        if sig.why_not:
            print("  почему НЕ вход    :")
            for r in sig.why_not:
                print(f"     • {r}")
        if sig.quality.warnings:
            print("  замечания к данным:")
            for w in sig.quality.warnings:
                print(f"     • {w}")
        print()

    sig = analyze(snap, profile="aggressive", cfg=cfg, now=now)
    print("=" * 78)
    print("ФАКТОРЫ (агрессивный профиль):")
    for f in sorted(sig.factors, key=lambda x: -abs(x.contribution)):
        print(f"  [{f.category:11s}] {f.title:18s} {f.score:+.2f} × {f.weight:.1f} "
              f"= {f.contribution:+.2f}  | {f.note}")
    print()
    print("ОБЪЯСНЕНИЕ:")
    print(" ", sig.explanation)
    print()
    print("РАЗБОР ДЛЯ НОВИЧКА:")
    for title, text in beginner_breakdown(sig, cfg):
        print(f"  {title}: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
