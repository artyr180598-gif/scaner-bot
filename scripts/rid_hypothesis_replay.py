"""Offline 15m research replay. No synthetic order-book or probability claims.

Usage: python -m scripts.rid_hypothesis_replay CACHE_ROOT SYMBOL [SYMBOL ...]
Fixed sample: March-August 2026; July-August reported separately as holdout.
One position per symbol. Next-open execution, stop first on ambiguous candles,
2R target, 24h maximum hold, 10 bps per side, no averaging or parameter search.
This is an exit-policy experiment, not a replay of the production RID plan.
"""

import json
import sys
from pathlib import Path

from cryptopilot.models import Side
from cryptopilot.rid_hypotheses import detect_hypotheses
from scripts.rid_backtest import _parse


def replay(bars, eligible=None):
    records = []
    busy_until = -1
    for i in range(60, len(bars) - 97):
        if i <= busy_until:
            continue
        history = bars[i - 59 : i + 1]
        if any(
            b.open_time_ms - a.open_time_ms != 900000
            for a, b in zip(history, history[1:], strict=False)
        ):
            continue
        hypotheses = detect_hypotheses(history)
        if not hypotheses:
            continue
        if eligible is not None and not eligible(bars[i].open_time_ms):
            continue
        h = hypotheses[0]
        sign = 1 if h.side is Side.LONG else -1
        entry = bars[i + 1].open
        risk = sign * (entry - h.invalidation)
        if risk <= 0 or risk / entry > 0.045 or risk / entry < 0.0035:
            continue
        # Cancel if the next open already escaped the confirmation candle.
        if abs(entry - bars[i].close) > (bars[i].high - bars[i].low) * 0.5:
            continue
        target = entry + sign * 2 * risk
        outcome = "TIME"
        exit_price = bars[i + 96].close
        end = i + 96
        valid = True
        for j in range(i + 1, i + 97):
            b = bars[j]
            if b.open_time_ms - bars[j - 1].open_time_ms != 900000:
                valid = False
                break
            stop_hit = b.low <= h.invalidation if sign == 1 else b.high >= h.invalidation
            tp_hit = b.high >= target if sign == 1 else b.low <= target
            if stop_hit:
                exit_price = (
                    min(b.open, h.invalidation) if sign == 1 else max(b.open, h.invalidation)
                )
                outcome, end = "SL", j
                break
            if tp_hit:
                exit_price, outcome, end = target, "TP", j
                break
        if not valid:
            continue
        busy_until = end
        net = (sign * (exit_price - entry) - (entry + exit_price) * 0.001) / risk
        records.append(
            {"name": h.name, "timestamp": bars[i].open_time_ms, "net_r": net, "outcome": outcome}
        )
    return records


def summary(rows):
    values = [r["net_r"] for r in rows]
    return {
        "trades": len(rows),
        "net_r": round(sum(values), 3),
        "mean_r": round(sum(values) / len(values), 3) if values else None,
        "win_rate": round(sum(v > 0 for v in values) / len(values) * 100, 2) if values else None,
    }


def main():
    from datetime import UTC, datetime

    root = Path(sys.argv[1])
    report = {}
    split = int(datetime(2026, 7, 1, tzinfo=UTC).timestamp() * 1000)
    for symbol in sys.argv[2:]:
        bars = []
        for month in range(3, 9):
            path = root / symbol / f"{symbol}-15m-2026-{month:02}.zip"
            if not path.exists():
                raise FileNotFoundError(path)
            bars.extend(_parse(path.read_bytes()))
        bars = sorted({b.open_time_ms: b for b in bars}.values(), key=lambda b: b.open_time_ms)
        rows = replay(bars)
        report[symbol] = {
            name: {
                "all": summary([r for r in rows if r["name"] == name]),
                "holdout": summary(
                    [r for r in rows if r["name"] == name and r["timestamp"] >= split]
                ),
            }
            for name in ("BASE_BREAKOUT", "TREND_PAUSE")
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
