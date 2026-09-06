"""Frozen cross-asset transfer, March-August 2026 (six months).

Predeclared new symbols: LINK, NEAR, AVAX, ADA, DOT, LTC; no outcome-based removal.
Unchanged economic reclaim baseline, squeeze and VWAP filters from prior research.
New symbols for this experiment, shared market dates: not independent market regimes.
No parameter search; no martingale; costs 6/12 bps per side and funding proxy.
This is a candle model, NOT a PRIME/order-book or whole-exchange scanner backtest.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.indicator_quality_study import confirms
from scripts.long_backtest import download_month, parse_archive
from scripts.quality_uncertainty import day_bootstrap
from scripts.signal_quality_study import run, summarize
from scripts.timing_guard_study import load

SYMBOLS = ("LINKUSDT", "NEARUSDT", "AVAXUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT")
CACHE = Path("frozen-transfer-cache")


def loader(symbol):
    if symbol == "BTCUSDT":
        return load(symbol)
    rows = []
    for month in range(3, 9):
        path = CACHE / symbol / f"{symbol}-15m-2026-{month:02}.zip"
        rows.extend(parse_archive(path.read_bytes()))
    if not rows or any(
        b.open_time_ms - a.open_time_ms != 900000
        for a, b in zip(rows, rows[1:], strict=False)
    ):
        raise ValueError(f"Incomplete history: {symbol}")
    return rows


def main():
    jobs = [(s, f"2026-{m:02}") for s in SYMBOLS for m in range(3, 9)]

    def fetch(job):
        s, m = job
        data = download_month(s, m, CACHE)
        if data is None:
            raise ValueError(f"Missing archive: {s} {m}")
        return (f"{s}/{m}", hashlib.sha256(data).hexdigest())

    with ThreadPoolExecutor(max_workers=6) as pool:
        hashes = dict(pool.map(fetch, jobs))
    print("Downloaded and hashed", len(hashes), "archives", flush=True)
    filters = {
        f"economic_{k}": lambda f, i, side, k=k: confirms(k, f, i, side)
        for k in ("baseline", "squeeze", "vwap")
    }
    out = run(filters, "frozen_transfer_records.json", loader=loader, symbols=SYMBOLS)
    summary = {
        "protocol": __doc__,
        "promotion_allowed": False,
        "archive_sha256": hashes,
        "results": {},
    }
    for name, rows in out["records"].items():
        summary["results"][name] = {
            "all": summarize(rows),
            "uncertainty": day_bootstrap(rows),
            "by_symbol": {s: summarize([r for r in rows if r["symbol"] == s]) for s in SYMBOLS},
            "by_month": {
                m: summarize([r for r in rows if r["time"][:7] == m])
                for m in sorted({r["time"][:7] for r in rows})
            },
        }
    Path("frozen_transfer_summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
