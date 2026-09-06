"""Exploratory indicator ablation; fixed before this run, not independent validation.

Same six coins and March-August2026 data as prior studies. Each extra filter
is tested separately against economic reclaim; no combinations or tuning.
CMF is candle-based money-flow proxy, NOT actual order flow or a heatmap.
"""

import json
from pathlib import Path

import numpy as np

from scripts.quality_uncertainty import day_bootstrap
from scripts.signal_quality_study import run


def confirms(kind, f, i, side):
    if kind == "baseline":
        return True
    if side not in (-1, 1) or i < 4:
        return False
    if kind == "cmf":
        value = side * f.cmf20[i]
        return bool(np.isfinite(value) and value >= 0.05)
    if kind == "vwap":
        value = side * f.vwap_distance_atr[i]
        return bool(np.isfinite(value) and 0 <= value <= 1)
    if kind == "squeeze":
        values = f.keltner_squeeze_ratio[i - 4 : i]
        return bool(np.isfinite(values).all() and (values < 1).any())
    if kind == "rvol":
        value = f.relative_volume20[i]
        return bool(np.isfinite(value) and 1.2 <= value <= 3)
    raise ValueError(f"Unknown filter: {kind}")


def main():
    kinds = ("baseline", "cmf", "vwap", "squeeze", "rvol")
    filters = {
        f"economic_{kind}": lambda f, i, side, kind=kind: confirms(kind, f, i, side)
        for kind in kinds
    }
    out = run(filters, "indicator_quality_results.json")
    out["indicator_protocol"] = __doc__
    out["uncertainty"] = {name: day_bootstrap(rows) for name, rows in out["records"].items()}
    Path("indicator_quality_results.json").write_text(json.dumps(out, indent=2))
    summary = {k: v for k, v in out.items() if k != "records"}
    Path("indicator_quality_summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
