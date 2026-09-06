"""Fixed setup sequence test, not RID reconstruction or independent validation.

15m range from preceding 20 bars; at least 3 of prior 4 bars BB/KC <1.
First close through frozen boundary, open inside; range <=2 prior ATR and
extension <=0.5 ATR. Compare immediate entry with retest within 8 bars.
Retest touches +/-0.15 ATR zone and closes beyond level, extension <=0.5 ATR.
Close >0.25 ATR back inside cancels. Frozen opposite boundary is stop.
No HTF filters, parameter tuning, martingale, or automatic live promotion.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from cryptopilot.research import feature_arrays
from scripts.indicator_period_check import load_period
from scripts.quality_uncertainty import day_bootstrap
from scripts.signal_quality_study import simulate, summarize
from scripts.timing_guard_study import load


def events(bars, f):
    active = None
    output = []
    for i in range(220, len(bars)):
        bar = bars[i]
        if active is not None:
            side, level, stop, atr, start = active
            displacement = side * (bar.close - level)
            if i - start > 8 or displacement < -0.25 * atr:
                active = None
            else:
                touched = bar.low <= level + 0.15 * atr and bar.high >= level - 0.15 * atr
                if touched and 0 < displacement <= 0.5 * atr:
                    output.append(("retest", i, side, stop))
                    active = None
                continue  # never replace frozen setup with a moving range
        ratios = f.keltner_squeeze_ratio[i - 4 : i]
        atr = f.atr14[i - 1]
        if not np.isfinite(ratios).all() or sum(ratios < 1) < 3 or not np.isfinite(atr) or atr <= 0:
            continue
        prior = bars[i - 20 : i]
        high, low = max(b.high for b in prior), min(b.low for b in prior)
        if not low <= bar.open <= high or bar.high - bar.low > 2 * atr:
            continue
        side = 1 if bar.close > high else -1 if bar.close < low else 0
        if not side:
            continue
        level, stop = (high, low) if side == 1 else (low, high)
        if side * (bar.close - level) > 0.5 * atr:
            continue
        output.append(("break", i, side, stop))
        active = side, level, stop, atr, i
    return output


def main():
    report = dict(protocol=__doc__, promotion_allowed=False, periods={})
    for period, loader in (("2026-03_to_08", load), ("2025-09-06_to_2026-02", load_period)):
        records = {"break": [], "retest": []}
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"):
            bars = loader(symbol)
            f = feature_arrays(bars)
            available = {name: 0 for name in records}
            for name, i, side, stop in events(bars, f):
                if i + 1 >= len(bars) or i + 1 < available[name]:
                    continue
                if abs(bars[i + 1].open - bars[i].close) > 0.25 * f.atr14[i]:
                    continue
                trade = simulate(bars, i + 1, side, stop)
                if trade is None:
                    continue
                available[name] = trade["exit_index"] + 5
                time = datetime.fromtimestamp(bars[i + 1].open_time_ms / 1000, UTC).isoformat()
                records[name].append(dict(trade, symbol=symbol, time=time, side=side))
        report["periods"][period] = {
            name: dict(
                all=summarize(rows),
                uncertainty=day_bootstrap(rows),
                by_symbol={
                    s: summarize([r for r in rows if r["symbol"] == s])
                    for s in sorted({r["symbol"] for r in rows})
                },
            )
            for name, rows in records.items()
        }
        Path(f"break_retest_{period}_trades.json").write_text(json.dumps(records, indent=2))
        print(period, {name: summarize(rows) for name, rows in records.items()}, flush=True)
    Path("break_retest_summary.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
