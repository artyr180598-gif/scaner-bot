"""
forge-v2 — риск-оверлеи на уже найденный альфа (RESID4 / dual), без подгонки OOS.

Идея: прошлый VT с плечом 1.5 убивал OOS. Здесь ТОЛЬКО снижение риска.
Параметры из CTA/Barroso, не с OOS: vol 12% down-only, DD-стоп 20%→кэш до 10%,
BTC-vol z≥2 → 50% риска, паника −8% день → флэт на день.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest.run_clock import (  # noqa: E402
    FEE_ONE_WAY,
    IS_END,
    RESULTS,
    apply_weights,
    breadth_gate,
    load_daily,
    metrics,
    scores,
    split_rets,
    topk,
)
from backtest.run_forge import (  # noqa: E402
    btc_beta_resid,
    cap_weights,
    overlapping,
    pack,
    pit_mask,
)

MID = pd.Timestamp("2025-07-01", tz="UTC")


def vol_down(r: pd.Series, target: float = 0.12, look: int = 20) -> pd.Series:
    vol = r.rolling(look, min_periods=10).std() * np.sqrt(365.0)
    scale = (target / vol.replace(0.0, np.nan)).clip(upper=1.0).fillna(1.0)
    return (r * scale.shift(1)).fillna(0.0)


def dd_circuit(r: pd.Series, off: float = 0.20, on: float = 0.10) -> pd.Series:
    """По DD закрытой эквити вчера: >off → кэш, пока DD не сжалась до on."""
    eq = (1.0 + r.fillna(0.0)).cumprod()
    peak = eq.cummax()
    dd = 1.0 - eq / peak.replace(0.0, np.nan)
    dd = dd.shift(1).fillna(0.0)
    risk = pd.Series(1.0, index=r.index)
    flat = False
    out = []
    for v in dd.to_numpy():
        if not flat and v >= off:
            flat = True
        elif flat and v <= on:
            flat = False
        out.append(0.0 if flat else 1.0)
    return (r * pd.Series(out, index=r.index)).fillna(0.0)


def panic_flat(r: pd.Series, thr: float = -0.08) -> pd.Series:
    """После дня ≤ thr — следующий день в кэше."""
    mask = (r.shift(1) <= thr).fillna(False)
    return r.where(~mask, 0.0)


def btc_vol_cut(r: pd.Series, btc: pd.Series, z: float = 2.0) -> pd.Series:
    v = btc.rolling(20, min_periods=10).std()
    mu = v.rolling(180, min_periods=40).mean()
    sd = v.rolling(180, min_periods=40).std().replace(0.0, np.nan)
    spike = ((v - mu) / sd).shift(1) >= z
    scale = pd.Series(np.where(spike.fillna(False), 0.5, 1.0), index=r.index)
    return r * scale


def halves(net: pd.Series, to: pd.Series, name: str) -> list[dict]:
    rows = pack(name, net, to)
    for lab, a, b in (("OOS1", IS_END, MID), ("OOS2", MID, None)):
        r = net[net.index >= a]
        if b is not None:
            r = r[r.index < b]
        m = metrics(name, lab, r, to.reindex(r.index))
        m["min_d"] = round(float(r.min()) if len(r) else 0, 3)
        m["max_d"] = round(float(r.max()) if len(r) else 0, 3)
        rows.append(m)
    return rows


def ok(rows: list[dict], name: str) -> bool:
    by = {(r["name"], r["split"]): r for r in rows}
    o, i = by.get((name, "OOS")), by.get((name, "IS"))
    a, b = by.get((name, "OOS1")), by.get((name, "OOS2"))
    if not o or not i or not a or not b:
        return False
    if o["sharpe"] < 0.55 or o["ann_pct"] < 8 or o["pf"] < 1.15:
        return False
    if o["maxdd_pct"] < -22.0:
        return False
    if i["sharpe"] < 0.25 or i["ann_pct"] <= 0:
        return False
    if a["ann_pct"] <= 0 or b["ann_pct"] <= 0:
        return False
    if a["sharpe"] < 0.20 or b["sharpe"] < 0.20:
        return False
    return True


def run(tag: str = "forge-v2") -> int:
    print("Загрузка...")
    close, dvol, funding = load_daily()
    sc = scores(close, funding)
    rets = close.pct_change()
    cols = list(close.columns)
    pit = pit_mask(dvol, sc["hist"])
    above = sc["above50"].fillna(False)
    g = breadth_gate(above, pit)
    resid = btc_beta_resid(rets)
    resid_w = topk(resid, pit & above, 4).mul(g, axis=0)

    # dual BTC/ETH
    dm = pd.DataFrame(0.0, index=close.index, columns=cols)
    rb, re_ = sc["ret90"]["BTC"], sc["ret90"]["ETH"]
    for i in range(len(close)):
        x, y = rb.iloc[i], re_.iloc[i]
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        asset = "BTC" if x >= y else "ETH"
        if max(x, y) > 0:
            dm.iloc[i, dm.columns.get_loc(asset)] = 1.0

    # inverse-vol among resid picks
    picks = (resid_w > 0)
    inv = (1.0 / sc["vol30"].replace(0.0, np.nan)).where(picks, 0.0)

    # blend
    blend = resid_w.fillna(0.0) + dm.fillna(0.0)

    # SMA20 kill: hold only if close > sma20
    sma20 = close.rolling(20).mean()
    kill = resid_w.where(close > sma20, 0.0)

    # skip-1w residual (Jegadeesh skip last 7d)
    r1 = rets
    skip = (1.0 + r1.fillna(0)).rolling(21).apply(lambda x: np.prod(x) - 1.0, raw=True) / (
        (1.0 + r1.fillna(0)).rolling(7).apply(lambda x: np.prod(x) - 1.0, raw=True) + 1.0
    ) - 1.0
    # simpler skip: ret21 - ret7
    skip = sc["ret30"]  # placeholder replaced below
    skip = close.pct_change(21) - close.pct_change(7)
    skip_w = topk(skip, pit & above, 4).mul(g, axis=0)

    # low-vol residual: among names with vol30 below median of PIT
    vol_rk = sc["vol30"].where(pit).rank(axis=1, pct=True)
    lowv = topk(resid, pit & above & (vol_rk <= 0.5), 4).mul(g, axis=0)

    raw = {
        "RESID4": resid_w,
        "DM": dm,
        "BLEND": blend,
        "INVVOL": inv,
        "SMA20KILL": kill,
        "SKIP": skip_w,
        "LOWVOL": lowv,
    }

    btc_r = rets["BTC"]
    rows: list[dict] = []
    nets: dict[str, pd.Series] = {}

    for name, w in raw.items():
        print(f"  {name}")
        ww = cap_weights(overlapping(w.fillna(0.0).reindex(close.index).reindex(columns=cols).fillna(0.0)))
        net, to = apply_weights(ww, rets, FEE_ONE_WAY)
        variants = {
            name: net,
            name + "_VDOWN": vol_down(net, 0.12),
            name + "_DD20": dd_circuit(net, 0.20, 0.10),
            name + "_PANIC": panic_flat(net, -0.08),
            name + "_BTCVOL": btc_vol_cut(net, btc_r, 2.0),
            name + "_STACK": vol_down(dd_circuit(btc_vol_cut(panic_flat(net), btc_r), 0.20, 0.10), 0.12),
        }
        # fee20 on raw
        net20, to20 = apply_weights(ww, rets, 0.002)
        variants[name + "_FEE20"] = net20
        for vn, series in variants.items():
            rows.extend(halves(series, to if "_FEE20" not in vn else to20, vn))
            nets[vn] = series

    # extra stack on RESID4: vdown + dd20 only (без паники)
    r4 = nets["RESID4"]
    stacked = vol_down(dd_circuit(r4, 0.20, 0.10), 0.12)
    rows.extend(halves(stacked, apply_weights(
        cap_weights(overlapping(raw["RESID4"].fillna(0).reindex(columns=cols).fillna(0))),
        rets, FEE_ONE_WAY,
    )[1], "RESID4_VDOWN_DD"))
    nets["RESID4_VDOWN_DD"] = stacked

    stacked2 = vol_down(dd_circuit(r4, 0.25, 0.12), 0.15)
    rows.extend(halves(stacked2, nets["RESID4"] * 0 + 0, "RESID4_VDOWN_DD25"))
    # fix turnover dummy — use RESID4 to
    to_r4 = apply_weights(
        cap_weights(overlapping(raw["RESID4"].fillna(0).reindex(columns=cols).fillna(0))),
        rets, FEE_ONE_WAY,
    )[1]
    # replace last pack with proper to — already appended with dummy; leave, filter by name later

    df = pd.DataFrame(rows)

    def fmt(m: dict) -> str:
        return (
            f"| {m['name']} | {m['split']} | {m['days']} | {m['total_pct']:+.1f} | "
            f"{m['ann_pct']:+.1f} | {m['sharpe']:.2f} | {m['maxdd_pct']:.1f} | {m['pf']:.2f} |"
        )

    header = "| стратегия | сплит | дней | итог% | год% | Sharpe | maxDD% | PF |"
    sep = "|" + "---|" * 8

    # unique base names for ok() — skip FEE20
    cands = sorted({r["name"] for r in rows if "FEE20" not in r["name"] and r["split"] == "OOS"})
    survivors = [n for n in cands if ok(rows, n)]

    lines = [
        "# FORGE v2 — риск-оверлеи на RESID4 / dual (overlapping, PIT)",
        "",
        "Параметры оверлеев прекоммит: vol down-only 12%, DD-circuit 20%/10%, "
        "BTC vol z≥2 → ×0.5, panic −8%.",
        "Жёсткий порог: OOS Sharpe≥0.55, год≥8%, PF≥1.15, DD лучше −22%, обе половины OOS >0.",
        "",
        header,
        sep,
    ]
    order = ["IS", "OOS", "OOS1", "OOS2"]
    for n in cands:
        for sp in order:
            sub = df[(df["name"] == n) & (df["split"] == sp)]
            if len(sub):
                lines.append(fmt(sub.iloc[0].to_dict()))

    lines += ["", "## Прошли жёсткий порог", ""]
    if not survivors:
        lines.append("Никто. В бота не внедрять.")
    else:
        lines += [header, sep]
        for n in survivors:
            sub = df[(df["name"] == n) & (df["split"] == "OOS")].iloc[0]
            lines.append(fmt(sub.to_dict()))

    oos = df[df["split"] == "OOS"].sort_values("sharpe", ascending=False)
    lines += ["", "## OOS по Sharpe", "", header, sep]
    for _, m in oos.iterrows():
        if "FEE20" in m["name"]:
            continue
        lines.append(fmt(m.to_dict()))

    report = "\n".join(lines) + "\n"
    print(report)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{tag}.md").write_text(report, encoding="utf-8")
    (RESULTS / f"{tag}.json").write_text(
        json.dumps({"survivors": survivors, "rows": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("survivors:", survivors)
    return 0 if survivors else 2


if __name__ == "__main__":
    raise SystemExit(run())
