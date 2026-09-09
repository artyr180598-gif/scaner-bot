"""Bounded averaging counterfactual on identical RID entry opportunities.

NOT RID's known private rules. LONG only: equal-size tranches at initial-entry
-3%/-6%, TP average+3%, hard stop initial-entry-9%, max24h. Compare 0/1/2 adds
with equal maximum planned cash loss INCLUDING 10bps per side, not equal initial
quantity. No unbounded martingale. Signals overlap across these counterfactuals;
sum of R is not portfolio P&L. Funding and actual historical spread are absent.
"""

import json
import sys
from pathlib import Path

from scripts.rid_hypothesis_replay import replay
from scripts.rid_selection_study import describe, load_panel, metrics, selection


def simulate(entry, future, max_adds, cost=0.001):
    if max_adds not in (0, 1, 2) or entry <= 0 or len(future) != 96:
        raise ValueError("Require positive entry, 96 candles and 0/1/2 equal-size adds")
    stop = entry * 0.91
    levels = [entry * (1 - 0.03 * i) for i in range(max_adds + 1)]
    budget = sum(level - stop + (level + stop) * cost for level in levels)
    fills = [entry]
    worst = 0.0
    for index, bar in enumerate(future):
        if index and bar.open_time_ms - future[index - 1].open_time_ms != 900000:
            raise ValueError("Discontinuous forward data")
        # Stop executes before ambiguous add/target; already resting adds may
        # fill on the way down, so charge all crossed levels before exit.
        added = False
        while len(fills) < len(levels) and bar.low <= levels[len(fills)]:
            fills.append(levels[len(fills)])
            added = True
        average = sum(fills) / len(fills)
        marked = sum(bar.low - p - (bar.low + p) * cost for p in fills) / budget
        worst = min(worst, marked)
        if bar.low <= stop:
            exit_price, outcome = min(bar.open, stop), "SL"
        elif not added and bar.high >= average * 1.03:
            exit_price, outcome = average * 1.03, "TP"
        elif index == 95:
            exit_price, outcome = bar.close, "TIME"
        else:
            continue
        net = sum(exit_price - p - (exit_price + p) * cost for p in fills) / budget
        return {"net_r": net, "outcome": outcome, "adds": len(fills) - 1, "worst_mark_r": worst}
    raise AssertionError("Unreachable")


def main():
    panel = load_panel(Path(sys.argv[1]))
    cost = float(sys.argv[2]) / 10000 if len(sys.argv) > 2 else 0.001
    measured = {symbol: metrics(bars) for symbol, bars in panel.items()}
    report = {}
    for variant in ("baseline", "combined"):
        outcomes = {n: [] for n in (0, 1, 2)}
        for symbol, bars in panel.items():
            if symbol == "BTCUSDT":
                continue
            opportunities = replay(
                bars, lambda t, s=symbol, v=variant: selection(measured, s, t, v)
            )
            indices = {b.open_time_ms: i for i, b in enumerate(bars)}
            for row in opportunities:
                i = indices[row["timestamp"]]
                future = bars[i + 1 : i + 97]
                if any(
                    b.open_time_ms - a.open_time_ms != 900000
                    for a, b in zip(bars[i : i + 96], future, strict=True)
                ):
                    continue
                for adds in outcomes:
                    outcomes[adds].append(simulate(future[0].open, future, adds, cost=cost))
        report[variant] = {
            str(n): {**describe(rows), "with_adds": sum(r["adds"] > 0 for r in rows)}
            for n, rows in outcomes.items()
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
