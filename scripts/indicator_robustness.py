"""Descriptive stability tests, not independent validation or portfolio returns."""

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from scripts.signal_quality_study import summarize


def paired_day_difference(baseline, candidate, repetitions=5000, seed=607):
    """Pair calendar entry days; report difference in summed R/day, not per trade."""
    totals = defaultdict(lambda: [0.0, 0.0])
    for group, rows in enumerate((baseline, candidate)):
        for row in rows:
            totals[row["time"][:10]][group] += row["net_r"]
    if not totals:
        return {"active_days": 0, "ci95_r_per_active_day": None}
    differences = np.array([totals[d][1] - totals[d][0] for d in sorted(totals)])
    indexes = np.random.default_rng(seed).integers(
        0, len(differences), (repetitions, len(differences))
    )
    return {
        "active_days": len(differences),
        "mean_difference_r_per_active_day": float(differences.mean()),
        "ci95_r_per_active_day": np.quantile(
            differences[indexes].mean(axis=1), [0.025, 0.975]
        ).tolist(),
        "limitations": "Only days active in either variant; summed trade R, not capital "
        "returns. Ignores dependence across entry days and multiple comparisons.",
    }


def stability(rows):
    return {
        "all": summarize(rows),
        "leave_one_month_out": {
            str(month): summarize([r for r in rows if r["month"] != month]) for month in range(4, 9)
        },
        "leave_one_coin_out": {
            coin: summarize([r for r in rows if r["symbol"] != coin])
            for coin in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")
        },
        "by_side": {
            str(side): summarize([r for r in rows if r["side"] == side]) for side in (-1, 1)
        },
    }


def main():
    source = json.loads(Path("indicator_quality_results.json").read_text())
    baseline = source["records"]["economic_baseline"]
    results = {
        name: dict(stability(rows), paired_to_baseline=paired_day_difference(baseline, rows))
        for name, rows in source["records"].items()
    }
    out = {"protocol": __doc__, "promotion_allowed": False, "results": results}
    Path("indicator_robustness_results.json").write_text(json.dumps(out, indent=2))
    for name, result in results.items():
        print(name, json.dumps(result["paired_to_baseline"]))
        print(
            "worst exclusion means",
            {
                key: min(v["net_r"]["mean"] for v in result[key].values() if v["n"])
                for key in ("leave_one_month_out", "leave_one_coin_out")
            },
        )


if __name__ == "__main__":
    main()
