"""Fixed point-in-time selection experiment; not a whole-exchange backtest.

Six prechosen liquid coins, two already researched recent periods. For each
direction rank 5h relative return vs BTC and low ATR-regime ratio equally.
Keep top two only; require at least four valid symbols and RVOL <=1.5.
No tuning, no averaging, unchanged economic reclaim entries and exits.
"""

import bisect
import json
import math
from pathlib import Path

from cryptopilot.research import feature_arrays
from scripts.indicator_period_check import load_period
from scripts.quality_uncertainty import day_bootstrap
from scripts.signal_quality_study import run, summarize
from scripts.timing_guard_study import load

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")


def rank_coins(snapshot, side, limit=2):
    """Snapshot maps symbol to (return%, ATR regime, RVOL); higher score wins."""
    if side not in (-1, 1) or "BTCUSDT" not in snapshot:
        return []
    btc = snapshot["BTCUSDT"][0]
    if not math.isfinite(btc):
        return []
    eligible = {
        s: v
        for s, v in snapshot.items()
        if all(math.isfinite(x) for x in v) and v[1] > 0 and 0 < v[2] <= 1.5
    }
    if len(eligible) < 4:
        return []
    # Pairwise ranks avoid mixing incompatible measurement scales. Ties equal.
    scores = {}
    for symbol, (ret, atr, _) in eligible.items():
        scores[symbol] = sum(
            int(side * (ret - btc) > side * (other[0] - btc)) + int(atr < other[1])
            for other in eligible.values()
        )
    return sorted(scores, key=lambda s: (-scores[s], s))[:limit]


class HistoricalSelector:
    def __init__(self, loader):
        self.data = {}
        for symbol in SYMBOLS:
            bars = loader(symbol)
            self.data[symbol] = ([b.open_time_ms + 900000 for b in bars], feature_arrays(bars))
        self.audit = {}

    def selected(self, signal_ms, side):
        key = f"{signal_ms}:{side}"
        if key not in self.audit:
            snapshot = {}
            for symbol, (times, f) in self.data.items():
                index = bisect.bisect_right(times, signal_ms) - 1
                if index < 220 or times[index] != signal_ms:
                    continue
                snapshot[symbol] = (
                    float(f.return_20_pct[index]),
                    float(f.atr_regime_ratio[index]),
                    float(f.relative_volume20[index]),
                )
            self.audit[key] = dict(selected=rank_coins(snapshot, side), snapshot=snapshot)
        return self.audit[key]["selected"]

    def gate(self, name, symbol, signal_ms, side):
        return name == "economic_all" or symbol in self.selected(signal_ms, side)


def main():
    result = {"protocol": __doc__, "promotion_allowed": False, "periods": {}}
    for label, loader in (("2026-03_to_08", load), ("2025-09-06_to_2026-02", load_period)):
        selector = HistoricalSelector(loader)
        variants = {"economic_all": lambda *args: True, "economic_selected": lambda *args: True}
        out = run(variants, f"selection_{label}_records.json", loader, selector.gate)
        results = {}
        for name, rows in out["records"].items():
            results[name] = dict(
                all=summarize(rows),
                uncertainty=day_bootstrap(rows),
                by_symbol={s: summarize([r for r in rows if r["symbol"] == s]) for s in SYMBOLS},
                by_month={
                    m: summarize([r for r in rows if r["time"][:7] == m])
                    for m in sorted({r["time"][:7] for r in rows})
                },
            )
        result["periods"][label] = results
        Path(f"selection_{label}_audit.json").write_text(json.dumps(selector.audit))
    Path("coin_selection_summary.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {p: {n: r["all"] for n, r in v.items()} for p, v in result["periods"].items()}, indent=2
        )
    )


if __name__ == "__main__":
    main()
