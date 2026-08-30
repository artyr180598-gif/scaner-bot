"""
run_backtest.py — CLI-прогон бектеста: OLD (плоский порог v2) vs NEW (квантовый
движок v3) на реальных исторических данных (Binance spot+perp+funding).

Примеры:
  .venv/bin/python backtest/run_backtest.py
  .venv/bin/python backtest/run_backtest.py --symbols BTC,ETH,SOL --years 2
  .venv/bin/python backtest/run_backtest.py --z-entry 1.5 --min-net-rt 0.15 --tag tune1

Отчёт: печать + backtest/results/<tag>.md и <tag>.json (история прогонов —
это «память» для будущих агентов, см. AI_AGENTS/BACKTESTS.md).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.engine import BacktestSimulator, build_4h_frames, load_symbol  # noqa: E402
from strategy import StrategyConfig  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Бектест спот↔перп арбитража")
    p.add_argument("--symbols", default="", help="список через запятую; пусто = все в кеше")
    p.add_argument("--years", type=float, default=0.0, help="сколько лет от конца данных")
    p.add_argument("--start", default="", help="YYYY-MM-DD")
    p.add_argument("--end", default="", help="YYYY-MM-DD")
    p.add_argument("--fee-spot", type=float, default=0.10, help="taker спот, %")
    p.add_argument("--fee-fut", type=float, default=0.05, help="taker перп, %")
    p.add_argument("--slippage-bps", type=float, default=2.5, help="half-spread на ногу, bps")
    p.add_argument("--alloc", type=float, default=0.05, help="доля капитала на CARRY-сделку")
    p.add_argument("--alloc-rev", type=float, default=0.10, help="доля капитала на REVERSION-сделку")
    p.add_argument("--max-concurrent", type=int, default=16)
    p.add_argument("--max-hold-hours", type=float, default=1080.0)
    p.add_argument("--z-exit", type=float, default=0.0)
    p.add_argument("--take-profit", type=float, default=1.5, help="TP от номинала, %")
    p.add_argument("--stop-loss", type=float, default=2.0, help="SL от номинала, %")
    p.add_argument("--flip-hours", type=float, default=72.0, help="окно трейлинг-funding для выхода")
    p.add_argument("--flip-threshold", type=float, default=0.10, help="платим больше этого за окно → выход, %")
    p.add_argument("--converged-min", type=float, default=0.5, help="мин. профит для досрочного выхода по сходимости, %")
    p.add_argument("--naive-funding", type=float, default=0.01, help="порог CARRY_NAIVE, %/8ч")
    p.add_argument("--old-threshold", type=float, default=2.0, help="порог OLD-стратегии, %")
    # параметры NEW-движка
    p.add_argument("--z-entry", type=float, default=1.5)
    p.add_argument("--z-entry-min", type=float, default=-1.0)
    p.add_argument("--pct-entry", type=float, default=90.0)
    p.add_argument("--min-net-rt", type=float, default=0.20)
    p.add_argument("--min-funding-edge", type=float, default=0.30)
    p.add_argument("--persistence", type=int, default=2)
    p.add_argument("--min-history", type=int, default=90)
    p.add_argument("--window-days", type=int, default=60)
    p.add_argument("--horizon-hours", type=float, default=240.0)
    p.add_argument("--max-halflife-hours", type=float, default=0.0)
    p.add_argument("--tag", default="")
    return p.parse_args()


def _date_to_ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def main() -> int:
    args = parse_args()
    dataset = RESULTS.parent / "data_cache" / "dataset"
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        or sorted(p.name.removesuffix(".csv.gz") for p in (dataset / "perp_4h").glob("*.csv.gz"))
    )
    symbols = [s for s in symbols if (dataset / "perp_4h" / f"{s}.csv.gz").exists()]

    frames: dict[str, pd.DataFrame] = {}
    funding: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            data = load_symbol(sym)
        except FileNotFoundError:
            continue
        frame = build_4h_frames(sym, data)
        if len(frame) < 500:
            continue
        frames[sym] = frame
        funding[sym] = data["funding"]

    if not frames:
        print("Нет данных. Запусти backtest/fetch_data.sh и backtest/prepare_data.py")
        return 1

    start_ts = _date_to_ms(args.start) if args.start else 0
    end_ts = _date_to_ms(args.end) if args.end else 0
    if args.years > 0:
        last = max(f.index[-1] for f in frames.values())
        start_ts = max(start_ts, last - int(args.years * 365 * 86400000))

    new_cfg = StrategyConfig(
        mode="adaptive",
        history_seconds=args.window_days * 86400.0,
        min_history=args.min_history,
        min_persistence=args.persistence,
        z_entry=args.z_entry,
        z_entry_min=args.z_entry_min,
        pct_entry=args.pct_entry,
        min_net_roundtrip_percent=args.min_net_rt,
        min_funding_edge_percent=args.min_funding_edge,
        horizon_hours=args.horizon_hours,
        max_halflife_hours=args.max_halflife_hours,
        spot_taker_fee_percent=args.fee_spot,
        futures_taker_fee_percent=args.fee_fut,
    )

    sim = BacktestSimulator(
        frames, funding,
        fee_spot_pct=args.fee_spot, fee_fut_pct=args.fee_fut,
        slippage_bps=args.slippage_bps, alloc_fraction=args.alloc,
        rev_alloc_fraction=args.alloc_rev,
        max_concurrent=args.max_concurrent,
        old_threshold_pct=args.old_threshold,
        naive_funding_pct=args.naive_funding,
        new_cfg=new_cfg,
        max_hold_hours=args.max_hold_hours, z_exit=args.z_exit,
        take_profit_pct=args.take_profit, stop_loss_pct=args.stop_loss,
        funding_flip_hours=args.flip_hours,
        funding_flip_threshold_pct=args.flip_threshold,
        converged_min_pct=args.converged_min,
        start_ts=start_ts, end_ts=end_ts,
    )
    results = sim.run()

    rows = []
    for name in ("OLD", "CARRY_NAIVE", "NEW_CARRYONLY", "NEW_REVONLY", "NEW"):
        m = results[name].metrics(args.alloc if name != "NEW_REVONLY" else args.alloc_rev)
        m["profit_factor"] = (
            str(m["profit_factor"]) if math.isinf(m["profit_factor"]) else m["profit_factor"]
        )
        rows.append(m)

    first_ts = min(f.index[0] for f in frames.values())
    last_ts = max(f.index[-1] for f in frames.values())
    period = f"{datetime.fromtimestamp(first_ts/1000, tz=timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc):%Y-%m-%d}"
    eff_start = max(first_ts, start_ts)
    period_eff = f"{datetime.fromtimestamp(eff_start/1000, tz=timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc):%Y-%m-%d}"

    header = ["стратегия", "сделок", "winrate%", "avg pnl%", "итог%", "годовые%", "maxDD%", "PF", "удерж.ч", "funding%", "fee%"]
    lines = [
        "# Бектест: спот↔перп (basis + funding carry)",
        "",
        f"- Период (эффективный): **{period_eff}** (данные: {period})",
        f"- Символов: {len(frames)} ({', '.join(list(frames)[:12])}{'…' if len(frames) > 12 else ''})",
        f"- Издержки: taker {args.fee_spot}%/{args.fee_fut}% (спот/перп), round-trip",
        f"  {args.fee_spot + args.fee_fut:.2f}%×2; проскальзывание {args.slippage_bps} bps/ногу",
        f"- Аллокация {args.alloc * 100:.0f}% капитала на сделку, ≤{args.max_concurrent} позиций",
        f"- NEW-параметры: z_min≥{args.z_entry_min}, pct≥{args.pct_entry}, edge≥{args.min_net_rt}%, "
        f"funding_edge≥{args.min_funding_edge}%, "
        f"persist≥{args.persistence}, history≥{args.min_history}, окно {args.window_days}д, "
        f"funding-горизонт {args.horizon_hours:.0f}ч, выход: TP {args.take_profit}% / "
        f"SL −{args.stop_loss}% / трейлинг-funding {args.flip_hours:.0f}ч ≤ −{args.flip_threshold}% / z≤{args.z_exit} / "
        f"тайм-стоп {args.max_hold_hours:.0f}ч",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    for m in rows:
        lines.append("| " + " | ".join(str(m[k]) for k in
                       ["strategy", "trades", "winrate_pct", "avg_pnl_pct", "total_return_pct",
                        "annualized_pct", "max_drawdown_pct", "profit_factor", "avg_hold_hours",
                        "avg_funding_pct", "avg_fees_pct"]) + " |")

    # топ-символы по pnl новой стратегии
    by_sym: dict[str, list[float]] = {}
    for t in results["NEW"].trades:
        by_sym.setdefault(t.sym, []).append(t.pnl_pct)
    top = sorted(by_sym.items(), key=lambda kv: sum(kv[1]), reverse=True)[:10]
    lines += ["", "## Топ-10 символов NEW (суммарный pnl% на 1 номинал)", "",
              "| символ | сделок | сумма pnl% | avg% |", "|---|---|---|---|"]
    for sym, pnls in top:
        lines.append(f"| {sym} | {len(pnls)} | {sum(pnls):+.2f} | {sum(pnls)/len(pnls):+.3f} |")

    # помесячная динамика NEW
    monthly: dict[str, float] = {}
    for t in results["NEW"].trades:
        label = datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        monthly[label] = monthly.get(label, 0.0) + t.pnl_pct
    lines += ["", "## NEW: pnl% по месяцам (на 1 номинал сделки)", "",
              "| месяц | pnl% |", "|---|---|"]
    for label in sorted(monthly):
        lines.append(f"| {label} | {monthly[label]:+.2f} |")

    report = "\n".join(lines)
    print(report)

    RESULTS.mkdir(exist_ok=True)
    tag = args.tag or datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"{tag}.md").write_text(report + "\n", encoding="utf-8")
    payload = {
        "tag": tag,
        "period": period_eff,
        "symbols": sorted(frames),
        "args": vars(args),
        "metrics": rows,
        "new_trades": [t.__dict__ for t in results["NEW"].trades],
    }
    (RESULTS / f"{tag}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nСохранено: backtest/results/{tag}.md / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
