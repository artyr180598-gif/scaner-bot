"""RID screenshot hypothesis, NOT identified RID rules.

Fixed 1h SMA14/28 (TradingView documentation example, not fitted to screenshot).
Compare crossover with crossover -> confirmed swing break -> pullback/reclaim.
Pivots require two right bars; never backdate their availability. Setup expires
12 hours after cross. Same six coins March-August2026; previously studied data.
15m execution replay: next open, 1h ATR/18x15m structural stop, TP2R, max72h,
6/12bps per side and funding proxy. No averaging, parameter search or live promotion.
"""

import bisect
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from cryptopilot.research import aggregate_candles, feature_arrays
from scripts.quality_uncertainty import day_bootstrap
from scripts.signal_quality_study import simulate, summarize
from scripts.timing_guard_study import load


def sma(values, period):
    out = np.full(len(values), np.nan)
    out[period - 1 :] = np.convolve(values, np.ones(period) / period, mode="valid")
    return out


def confirmed_pivot(bars, now, side):
    center = now - 2
    if center < 2:
        return None
    value = bars[center].high if side == 1 else bars[center].low
    others = [
        bars[k].high if side == 1 else bars[k].low
        for k in range(center - 2, now + 1)
        if k != center
    ]
    return (
        value
        if (all(value > x for x in others) if side == 1 else all(value < x for x in others))
        else None
    )


def events(bars):
    close = np.array([b.close for b in bars])
    fast, slow = sma(close, 14), sma(close, 28)
    levels = {1: None, -1: None}
    active = None
    output = []
    for i in range(4, len(bars)):
        for side in (1, -1):
            pivot = confirmed_pivot(bars, i, side)
            if pivot is not None:
                levels[side] = pivot
        if i < 28:
            continue
        direction = 1 if fast[i] > slow[i] else -1 if fast[i] < slow[i] else 0
        cross = direction != 0 and direction * (fast[i - 1] - slow[i - 1]) <= 0
        if cross:
            output.append(("cross", i, direction))
            active = dict(side=direction, start=i, level=levels[direction], stage="BREAK")
        if active is None:
            continue
        side = active["side"]
        if i - active["start"] > 12 or direction != side:
            active = None
            continue
        stage = active["stage"]
        if stage == "BREAK" and active["level"] is not None:
            if side * (close[i] - active["level"]) > 0:
                active["stage"] = "PULLBACK"
        elif stage == "PULLBACK" and side * (close[i] - fast[i]) <= 0:
            active["stage"] = "RECLAIM"
        elif stage == "RECLAIM" and side * (close[i] - fast[i]) > 0:
            output.append(("structure_reclaim", i, side))
            active = None
    return output


def main():
    records = {"cross": [], "structure_reclaim": []}
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"):
        bars = load(symbol)
        hourly = aggregate_candles(bars, 60)
        features = feature_arrays(hourly)
        times = [b.open_time_ms for b in bars]
        available = {name: 0 for name in records}
        for name, index, side in events(hourly):
            if index < 220:
                continue
            signal_ms = hourly[index].open_time_ms + 3600000
            entry_index = bisect.bisect_left(times, signal_ms)
            if entry_index >= len(bars) or times[entry_index] != signal_ms:
                continue
            if entry_index < available[name]:
                continue
            entry = bars[entry_index].open
            atr = features.atr14[index]
            if not np.isfinite(atr) or atr <= 0 or abs(entry - hourly[index].close) > 0.25 * atr:
                continue
            history = bars[entry_index - 18 : entry_index]
            stop = (
                min(min(b.low for b in history), entry - 1.45 * atr)
                if side == 1
                else max(max(b.high for b in history), entry + 1.45 * atr)
            )
            result = simulate(bars, entry_index, side, stop)
            if result is None:
                continue
            available[name] = result["exit_index"] + 5
            date = datetime.fromtimestamp(signal_ms / 1000, UTC)
            records[name].append(
                dict(result, symbol=symbol, side=side, time=date.isoformat(), month=date.month)
            )
    out = dict(
        protocol=__doc__,
        promotion_allowed=False,
        records=records,
        results={
            name: dict(
                all=summarize(rows),
                uncertainty=day_bootstrap(rows),
                by_month={
                    str(m): summarize([r for r in rows if r["month"] == m]) for m in range(3, 9)
                },
            )
            for name, rows in records.items()
        },
    )
    Path("rid_overlay_results.json").write_text(json.dumps(out, indent=2))
    Path("rid_overlay_summary.json").write_text(
        json.dumps({k: v for k, v in out.items() if k != "records"}, indent=2)
    )
    print(json.dumps({name: result["all"] for name, result in out["results"].items()}, indent=2))


if __name__ == "__main__":
    main()
