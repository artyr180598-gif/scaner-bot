"""Frozen-rule transfer check: 2025-09-06 through 2026-02-28 UTC.

Different period from indicator selection, within the user's one-year history
limit. Archives existed in earlier broader research: NOT a pristine holdout.
No threshold changes; baseline, VWAP and squeeze only, chosen before this run.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.indicator_quality_study import confirms
from scripts.long_backtest import parse_archive
from scripts.quality_uncertainty import day_bootstrap
from scripts.signal_quality_study import run, summarize


def load_period(symbol):
    rows = []
    for year, month in [(2025, m) for m in range(9, 13)] + [(2026, 1), (2026, 2)]:
        path = Path("research-cache-v31") / symbol / f"{symbol}-15m-{year}-{month:02}.zip"
        rows.extend(parse_archive(path.read_bytes()))
    start = int(datetime(2025, 9, 6, tzinfo=UTC).timestamp() * 1000)
    rows = [r for r in rows if r.open_time_ms >= start]
    if not rows or any(
        b.open_time_ms - a.open_time_ms != 900000
        for a, b in zip(rows, rows[1:], strict=False)
    ):
        raise ValueError(f"Missing or non-contiguous history: {symbol}")
    return rows


def main():
    filters = {
        f"economic_{kind}": lambda f, i, side, kind=kind: confirms(kind, f, i, side)
        for kind in ("baseline", "vwap", "squeeze")
    }
    out = run(filters, "indicator_period_results.json", loader=load_period)
    out["protocol"] = __doc__
    months = ["2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]
    for name, rows in out["records"].items():
        out["results"][name]["by_month"] = {
            m: summarize([r for r in rows if r["time"][:7] == m]) for m in months
        }
    out["uncertainty"] = {n: day_bootstrap(r) for n, r in out["records"].items()}
    Path("indicator_period_results.json").write_text(json.dumps(out, indent=2))
    Path("indicator_period_summary.json").write_text(
        json.dumps({k: v for k, v in out.items() if k != "records"}, indent=2)
    )


if __name__ == "__main__":
    main()
