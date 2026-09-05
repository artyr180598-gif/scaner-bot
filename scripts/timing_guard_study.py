"""Fixed paired risk-filter test, March-August2026; no parameter tuning.

Core-engine historical approximation, not real scanner/derivatives executions.
Live-equivalent 72h holding cap,6bps/side. Same data previously researched; no
independent holdout. Safety filters are not an early-entry prediction model.
"""

import json
from pathlib import Path

from cryptopilot.research import MultiTimeframeResearchBacktester
from scripts.long_backtest import parse_archive


def load(symbol):
    rows = []
    for month in range(3, 9):
        path = Path("research-cache-v31") / symbol / f"{symbol}-15m-2026-{month:02}.zip"
        rows.extend(parse_archive(path.read_bytes()))
    assert all(
        b.open_time_ms - a.open_time_ms == 900000 for a, b in zip(rows, rows[1:], strict=False)
    )
    return rows


def run():
    btc = load("BTCUSDT")
    out = {"protocol": __doc__, "results": {}}
    for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]:
        bars = btc if symbol == "BTCUSDT" else load(symbol)
        results = {}
        for enabled in [False, True]:
            result = MultiTimeframeResearchBacktester(
                strict_entry_timing=enabled, max_holding_bars=288, one_way_cost_bps=6
            ).run(symbol, bars, btc)
            results["guard" if enabled else "baseline"] = result.to_dict()
        out["results"][symbol] = results
        print(
            symbol,
            {k: {"n": v["trades"], "expectancy_r": v["expectancy_r"]} for k, v in results.items()},
            flush=True,
        )
    Path("timing_guard_results.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    run()
