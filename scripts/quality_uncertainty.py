"""Reproduce uncertainty without changing strategy rules or selecting winners."""

import json
from collections import defaultdict
from pathlib import Path

import numpy as np


def day_bootstrap(rows, repetitions=5000, seed=606):
    groups = defaultdict(list)
    for row in rows:
        groups[row["time"][:10]].append(float(row["net_r"]))
    days = sorted(groups)
    if not days:
        return {"n": 0, "entry_days": 0, "mean_ci95_r": None}
    totals = np.array([sum(groups[d]) for d in days])
    counts = np.array([len(groups[d]) for d in days])
    draws = np.random.default_rng(seed).integers(0, len(days), (repetitions, len(days)))
    means = totals[draws].sum(axis=1) / counts[draws].sum(axis=1)
    return {
        "n": len(rows),
        "entry_days": len(days),
        "mean_ci95_r": np.quantile(means, [0.025, 0.975]).tolist(),
        "repetitions": repetitions,
        "seed": seed,
        "limitations": "Entry-day clusters do not capture all cross-day dependence; "
        "post-selection uncertainty remains. Not an independent validation.",
    }


def run():
    variants = defaultdict(list)
    for path in sorted(Path("quality-research-results").glob("*USDT.json")):
        for name, rows in json.loads(path.read_text()).items():
            variants[name].extend(rows)
    results = {name: day_bootstrap(rows) for name, rows in variants.items()}
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    run()
