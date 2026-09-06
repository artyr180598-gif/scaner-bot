"""Exploratory quality study, March-August2026; economic phase added posthoc.

Hypothesis: buying strength after an observed pullback/reclaim is different from
ranking mature trends. Compare reclaim with/without volume confirmation. Long
and short symmetric. Six liquid coins, same next-open fills, 1.45ATR/18-bar stop,
TP2R, max72h, 6/12bps each execution, funding proxy1bp/8h, no averaging/leverage.
Previously examined period: exploratory, NOT an independent holdout. Historical
spread/OI/funding/scanner membership unavailable. Monthly/cross-symbol breakdowns
must be retained; don't promote from one pooled positive number.
"""

import bisect
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from cryptopilot.research import aggregate_candles, feature_arrays
from scripts.timing_guard_study import load


def simulate(bars, start, side, stop, horizon=288):
    entry = bars[start].open
    risk = side * (entry - stop)
    if not 0.0025 <= risk / entry <= 0.05:
        return None
    target = entry + side * 2 * risk
    end = min(start + horizon - 1, len(bars) - 1)
    if end - start + 1 != horizon:
        return None
    reason = "TIME"
    for k in range(start, end + 1):
        b = bars[k]
        if b.low <= stop if side == 1 else b.high >= stop:
            exit_price = min(stop, b.open) if side == 1 else max(stop, b.open)
            reason = "SL"
            break
        if b.high >= target if side == 1 else b.low <= target:
            exit_price = target
            reason = "TP2"
            break
        exit_price = b.close
    ratio = exit_price / entry
    hours = (k - start + 1) / 4
    gross = side * (exit_price - entry) / risk
    funding = 0.0001 * hours / 8 * entry / risk
    return dict(
        exit_index=k,
        entry=entry,
        exit=exit_price,
        stop=stop,
        target=target,
        reason=reason,
        hours=hours,
        gross_r=gross,
        net_r=gross - 0.0006 * (entry + exit_price) / risk - funding,
        stress_r=gross - 0.0012 * (entry + exit_price) / risk - funding,
        net_pct=100 * (side * (ratio - 1) - 0.0006 * (1 + ratio) - 0.0001 * hours / 8),
    )


def summarize(rows):
    if not rows:
        return {"n": 0}
    out = {"n": len(rows), "median_hours": float(np.median([r["hours"] for r in rows]))}
    for field in ["gross_r", "net_r", "stress_r"]:
        x = np.array([r[field] for r in rows])
        out[field] = {
            "mean": float(x.mean()),
            "sum": float(x.sum()),
            "win_pct": float((x > 0).mean() * 100),
            "profit_factor": float(x[x > 0].sum() / -x[x < 0].sum()) if (x < 0).any() else None,
        }
    return out


def run(
    extra_filters=None, output_path="signal_quality_results.json", loader=load, candidate_gate=None
):
    btc = loader("BTCUSDT")
    btc4 = aggregate_candles(btc, 240)
    fb = feature_arrays(btc4)
    btc_times = [c.open_time_ms + 14400000 for c in btc4]
    records = {
        "reclaim": [],
        "reclaim_volume": [],
        "reclaim_economic": [],
        "reclaim_volume_economic": [],
    }
    if extra_filters is not None:
        records = {name: [] for name in extra_filters}
    for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]:
        bars = btc if symbol == "BTCUSDT" else loader(symbol)
        h1 = aggregate_candles(bars, 60)
        h4 = aggregate_candles(bars, 240)
        f = feature_arrays(bars)
        f1 = feature_arrays(h1)
        f4 = feature_arrays(h4)
        t1 = [c.open_time_ms + 3600000 for c in h1]
        t4 = [c.open_time_ms + 14400000 for c in h4]
        available = {name: 0 for name in records}
        for i in range(220, len(bars) - 289):
            now = bars[i].open_time_ms + 900000
            j = bisect.bisect_right(t1, now) - 1
            k = bisect.bisect_right(t4, now) - 1
            b = bisect.bisect_right(btc_times, now) - 1
            if min(j, k, b) < 210:
                continue
            side = 1 if f4.score[k] > 0 else -1
            if side * f4.score[k] < 25 or side * f1.score[j] < 25 or f1.adx14[j] < 20:
                continue
            if side * fb.score[b] < -25:
                continue
            # A real pullback must be observed before the current reclaim bar.
            if side * (bars[i - 1].close - f.ema20[i - 1]) > 0:
                continue
            if side * (bars[i].close - f.ema20[i]) <= 0:
                continue
            previous_rsi = f.rsi14[i - 1] if side == 1 else 100 - f.rsi14[i - 1]
            current_rsi = f.rsi14[i] if side == 1 else 100 - f.rsi14[i]
            if not 35 <= previous_rsi <= 55 or not 45 <= current_rsi <= 65:
                continue
            atr = f.atr14[i]
            if atr <= 0 or bars[i].high - bars[i].low > 1.5 * atr:
                continue
            if abs(bars[i].close - f.ema20[i]) > atr:
                continue
            entry = bars[i + 1].open
            if abs(entry - bars[i].close) > 0.25 * atr:
                continue
            stop = (
                min(min(c.low for c in bars[i - 17 : i + 1]), entry - 1.45 * atr)
                if side == 1
                else max(max(c.high for c in bars[i - 17 : i + 1]), entry + 1.45 * atr)
            )
            for name in records:
                if candidate_gate is not None and not candidate_gate(name, symbol, now, side):
                    continue
                if extra_filters is not None and not extra_filters[name](f, i, side):
                    continue
                if i + 1 < available[name] or ("volume" in name and f.relative_volume20[i] < 1.2):
                    continue
                result = simulate(bars, i + 1, side, stop)
                if result is None:
                    continue
                if "economic" in name:
                    risk = side * (entry - stop)
                    reward = side * (result["target"] - entry)
                    net_rr = (reward - 0.0006 * (entry + result["target"])) / (
                        risk + 0.0006 * (entry + stop)
                    )
                    if net_rr < 1.8:
                        continue
                available[name] = result["exit_index"] + 1 + 4
                date = datetime.fromtimestamp(now / 1000, UTC)
                records[name].append(
                    dict(result, symbol=symbol, time=date.isoformat(), month=date.month, side=side)
                )
        print(symbol, {n: len(r) for n, r in records.items()}, flush=True)
    out = {
        "protocol": __doc__,
        "phase2_disclosure": "Economic variants added AFTER original outcomes showed cost drag; "
        "a posthoc subset looked positive. Threshold1.8 is the existing configured RR minimum, "
        "now applied net of costs. This rerun gives each variant independent trade occupancy. "
        "NOT an independent validation; no promotion is allowed on these data.",
        "promotion_allowed": False,
        "records": records,
        "results": {
            n: {
                "all": summarize(r),
                "by_month": {
                    str(m): summarize([x for x in r if x["month"] == m]) for m in range(3, 9)
                },
                "by_symbol": {
                    s: summarize([x for x in r if x["symbol"] == s])
                    for s in sorted({x["symbol"] for x in r})
                },
            }
            for n, r in records.items()
        },
    }
    Path(output_path).write_text(json.dumps(out, indent=2))
    if extra_filters is None:
        export(out)
    print(json.dumps({n: v["all"] for n, v in out["results"].items()}, indent=2))
    return out


def export(out):
    folder = Path("quality-research-results")
    folder.mkdir(exist_ok=True)
    summary = {k: v for k, v in out.items() if k != "records"}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))
    symbols = sorted({r["symbol"] for rows in out["records"].values() for r in rows})
    for symbol in symbols:
        (folder / f"{symbol}.json").write_text(
            json.dumps(
                {
                    name: [r for r in rows if r["symbol"] == symbol]
                    for name, rows in out["records"].items()
                },
                separators=(",", ":"),
            )
        )


if __name__ == "__main__":
    run()
