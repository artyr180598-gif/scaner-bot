"""Fixed RID selection ablation on existing archives; no production mutations.

Eight-symbol historical panel, not a whole-exchange historical universe.
July-August is already inspected data: diagnostic split, NOT untouched holdout.
Selection uses only bars closed at the signal timestamp and a full 100-bar
continuous history. Next-open execution and exit rules match the base replay.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.rid_backtest import _parse
from scripts.rid_hypothesis_replay import replay, summary

SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "NEARUSDT",
    "ADAUSDT",
    "AVAXUSDT",
)


def load_panel(root):
    panel = {}
    for symbol in SYMBOLS:
        cache = "research-cache-v31" if symbol in SYMBOLS[:4] else "frozen-transfer-cache"
        rows = []
        for month in range(3, 9):
            file = root / cache / symbol / f"{symbol}-15m-2026-{month:02}.zip"
            rows.extend(_parse(file.read_bytes()))
        panel[symbol] = sorted(
            {b.open_time_ms: b for b in rows}.values(), key=lambda b: b.open_time_ms
        )
    return panel


def metrics(bars):
    output = {}
    for i in range(99, len(bars)):
        window = bars[i - 99 : i + 1]
        if any(
            b.open_time_ms - a.open_time_ms != 900000
            for a, b in zip(window, window[1:], strict=False)
        ):
            continue
        old = sum(b.turnover for b in window[:-4]) / 24
        if old <= 0 or bars[i - 16].close <= 0:
            continue
        output[bars[i].open_time_ms] = (
            bars[i].close / bars[i - 16].close - 1,
            sum(b.turnover for b in window[-4:]) / old,
        )
    return output


def selection(panel_metrics, symbol, timestamp, variant):
    # Matched coverage: baseline and every variant require the same panel data.
    if any(timestamp not in rows for rows in panel_metrics.values()):
        return False
    ret, turnover = panel_metrics[symbol][timestamp]
    btc_return = panel_metrics["BTCUSDT"][timestamp][0]
    peer_returns = sorted(rows[timestamp][0] for rows in panel_metrics.values())
    strong = ret > 0 and ret > btc_return and ret >= peer_returns[-2]
    active = turnover >= 1.5
    return {
        "baseline": True,
        "relative_strength": strong,
        "turnover": active,
        "combined": strong and active,
    }[variant]


def describe(rows):
    result = summary(rows)
    # Sum-of-R is not a portfolio equity curve; simultaneous exposures remain.
    result["worst_trade_r"] = round(min((r["net_r"] for r in rows), default=0), 3)
    return result


def main():
    panel = load_panel(Path(sys.argv[1]))
    measured = {symbol: metrics(bars) for symbol, bars in panel.items()}
    split = int(datetime(2026, 7, 1, tzinfo=UTC).timestamp() * 1000)
    report = {}
    for variant in ("baseline", "relative_strength", "turnover", "combined"):
        rows = []
        by_symbol = {}
        for symbol, bars in panel.items():
            if symbol == "BTCUSDT":
                continue  # BTC is benchmark only in every arm, including baseline.
            records = replay(bars, lambda t, s=symbol, v=variant: selection(measured, s, t, v))
            rows.extend(records)
            by_symbol[symbol] = describe(records)
        report[variant] = {
            "all": describe(rows),
            "march_june": describe([r for r in rows if r["timestamp"] < split]),
            "july_august_seen": describe([r for r in rows if r["timestamp"] >= split]),
            "by_symbol": by_symbol,
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
