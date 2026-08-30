"""
forge-v3 — дожим LOWVOL: свои выходы/веса, без ухода в вечный кэш.

Порог «можно показывать как рабочее»:
  OOS Sharpe≥0.55, год≥8%, PF≥1.15, DD лучше −22%,
  обе половины OOS >0, fee20 Sharpe≥0.35, доля дней в рынке ≥40%.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path := __import__("pathlib").Path(__file__).resolve().parents[1].as_posix()))
sys.path.insert(0, ".")

from backtest.run_clock import (  # noqa: E402
    FEE_ONE_WAY,
    IS_END,
    RESULTS,
    apply_weights,
    breadth_gate,
    load_daily,
    metrics,
    scores,
    topk,
)
from backtest.run_forge import btc_beta_resid, cap_weights, overlapping, pit_mask  # noqa: E402
from backtest.run_forge2 import MID, halves, vol_down  # noqa: E402

IN_MKT_MIN = 0.40


def in_mkt(r: pd.Series) -> float:
    return float((r.abs() > 1e-12).mean())


def chandelier_mask(close: pd.DataFrame, atr_n: int = 14, k: float = 2.5) -> pd.DataFrame:
    prev = close.shift(1)
    tr = pd.concat(
        [
            (close - close.shift(1)).abs(),
            (close.shift(1) * 0 + 0),  # placeholder
        ],
        axis=1,
    )
    # ATR proxy: rolling mean of |ret|*close
    atr = close.pct_change().abs().rolling(atr_n, min_periods=5).mean() * close
    hh = close.rolling(20, min_periods=5).max()
    return close >= (hh - k * atr)


def name_trail_ok(close: pd.DataFrame, drop: float = 0.12) -> pd.DataFrame:
    hh = close.rolling(20, min_periods=5).max()
    return close >= hh * (1.0 - drop)


def invvol_weights(picks: pd.DataFrame, vol: pd.DataFrame) -> pd.DataFrame:
    w = (1.0 / vol.replace(0.0, np.nan)).where(picks > 0, 0.0)
    return w.fillna(0.0)


def adapt_k(score, mask, breadth: pd.Series) -> pd.DataFrame:
    """k=2 если рынок узкий, k=4 если широкий."""
    w2 = topk(score, mask, 2)
    w4 = topk(score, mask, 4)
    use4 = (breadth >= 0.55).astype(float)
    return w4.mul(use4, axis=0) + w2.mul(1.0 - use4, axis=0)


def book_rp(a: pd.Series, b: pd.Series, target: float = 0.10) -> pd.Series:
    """Две книги, каждая down-vol, потом 50/50."""
    sa = vol_down(a, target)
    sb = vol_down(b, target)
    return 0.5 * sa + 0.5 * sb


def pass_bar(rows: list[dict], name: str, net: pd.Series) -> bool:
    by = {(r["name"], r["split"]): r for r in rows}
    o, i = by.get((name, "OOS")), by.get((name, "IS"))
    a, b = by.get((name, "OOS1")), by.get((name, "OOS2"))
    if not all((o, i, a, b)):
        return False
    if o["sharpe"] < 0.55 or o["ann_pct"] < 8 or o["pf"] < 1.15:
        return False
    if o["maxdd_pct"] < -22.0:
        return False
    if i["sharpe"] < 0.25 or i["ann_pct"] <= 0:
        return False
    if a["ann_pct"] <= 0 or b["ann_pct"] <= 0:
        return False
    oos = net[net.index >= IS_END]
    if in_mkt(oos) < IN_MKT_MIN:
        return False
    return True


def run(tag: str = "forge-v3") -> int:
    print("Загрузка...")
    close, dvol, funding = load_daily()
    sc = scores(close, funding)
    rets = close.pct_change()
    cols = list(close.columns)
    pit = pit_mask(dvol, sc["hist"])
    above = sc["above50"].fillna(False)
    g = breadth_gate(above, pit)
    resid = btc_beta_resid(rets)
    vol_rk = sc["vol30"].where(pit).rank(axis=1, pct=True)
    quiet = pit & above & (vol_rk <= 0.5)
    low_eq = topk(resid, quiet, 4).mul(g, axis=0)

    # rank by residual sharpe (resid / vol)
    rsh = resid / sc["vol30"].replace(0.0, np.nan)
    low_sh = topk(rsh, quiet, 4).mul(g, axis=0)

    # funding size overlay
    fund_sz = low_eq * (1.0 - 0.25 * sc["fz"].fillna(0.0).clip(-2, 2))

    # chandelier / trail
    chan = low_eq.where(chandelier_mask(close), 0.0)
    trail = low_eq.where(name_trail_ok(close, 0.12), 0.0)

    # inverse vol among picks
    iv = invvol_weights(low_eq, sc["vol30"])

    # adaptive k
    br = (above & pit).sum(axis=1) / pit.sum(axis=1).replace(0, np.nan)
    kad = adapt_k(resid, quiet, br.fillna(0)).mul(g, axis=0)

    # skip last week
    skip = close.pct_change(21) - close.pct_change(7)
    low_skip = topk(skip, quiet, 4).mul(g, axis=0)

    # dual
    dm = pd.DataFrame(0.0, index=close.index, columns=cols)
    rb, re_ = sc["ret90"]["BTC"], sc["ret90"]["ETH"]
    for i in range(len(close)):
        x, y = rb.iloc[i], re_.iloc[i]
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        asset = "BTC" if x >= y else "ETH"
        if max(x, y) > 0:
            dm.iloc[i, dm.columns.get_loc(asset)] = 1.0

    raw = {
        "LOWVOL": low_eq,
        "LOW_SHARPE": low_sh,
        "LOW_FUND": fund_sz,
        "LOW_CHAN": chan,
        "LOW_TRAIL": trail,
        "LOW_IVOL": iv,
        "LOW_KADAPT": kad,
        "LOW_SKIP": low_skip,
        "DM": dm,
        "LOW_DM": low_eq.fillna(0) + dm.fillna(0),
    }

    rows: list[dict] = []
    nets: dict[str, pd.Series] = {}
    tos: dict[str, pd.Series] = {}

    def sim(name: str, w: pd.DataFrame, fee: float = FEE_ONE_WAY):
        ww = cap_weights(overlapping(w.fillna(0.0).reindex(close.index).reindex(columns=cols).fillna(0.0)))
        return apply_weights(ww, rets, fee)

    for name, w in raw.items():
        print(" ", name)
        net, to = sim(name, w)
        nets[name] = net
        tos[name] = to
        rows.extend(halves(net, to, name))
        vd = vol_down(net, 0.15)
        nets[name + "_VD15"] = vd
        rows.extend(halves(vd, to, name + "_VD15"))
        net20, to20 = sim(name, w, 0.002)
        rows.extend(halves(net20, to20, name + "_FEE20"))

    # risk-parity two books: LOWVOL + DM, down-vol 10% each
    rp = book_rp(nets["LOWVOL"], nets["DM"], 0.10)
    nets["BOOK_RP"] = rp
    rows.extend(halves(rp, tos["LOWVOL"], "BOOK_RP"))

    # LOWVOL vd15 after trail
    tr_vd = vol_down(nets["LOW_TRAIL"], 0.15)
    nets["TRAIL_VD15"] = tr_vd
    rows.extend(halves(tr_vd, tos["LOW_TRAIL"], "TRAIL_VD15"))

    chan_vd = vol_down(nets["LOW_CHAN"], 0.15)
    nets["CHAN_VD15"] = chan_vd
    rows.extend(halves(chan_vd, tos["LOW_CHAN"], "CHAN_VD15"))

    iv_vd = vol_down(nets["LOW_IVOL"], 0.15)
    nets["IVOL_VD15"] = iv_vd
    rows.extend(halves(iv_vd, tos["LOW_IVOL"], "IVOL_VD15"))

    cands = sorted({r["name"] for r in rows if "FEE20" not in r["name"] and r["split"] == "OOS"})
    survivors = []
    for n in cands:
        series = nets.get(n)
        if series is None:
            continue
        if pass_bar(rows, n, series):
            survivors.append(n)

    def fmt(m):
        return (
            f"| {m['name']} | {m['split']} | {m['days']} | {m['total_pct']:+.1f} | "
            f"{m['ann_pct']:+.1f} | {m['sharpe']:.2f} | {m['maxdd_pct']:.1f} | {m['pf']:.2f} |"
        )

    header = "| стратегия | сплит | дней | итог% | год% | Sharpe | maxDD% | PF |"
    sep = "|" + "---|" * 8
    df = __import__("pandas").DataFrame(rows)
    lines = [
        "# FORGE v3 — LOWVOL дожим (свои выходы/веса)",
        "",
        "Порог: OOS Sharpe≥0.55 год≥8% PF≥1.15 DD лучше −22%, обе половины >0, в рынке ≥40%.",
        "",
        header,
        sep,
    ]
    for n in cands:
        for sp in ("IS", "OOS", "OOS1", "OOS2"):
            sub = df[(df["name"] == n) & (df["split"] == sp)]
            if len(sub):
                lines.append(fmt(sub.iloc[0].to_dict()))

    lines += ["", "## В рынке (OOS)", ""]
    for n in cands:
        s = nets.get(n)
        if s is None:
            continue
        lines.append(f"- {n}: {100*in_mkt(s[s.index>=IS_END]):.0f}% дней")

    lines += ["", "## Прошли порог (и не кэш)", ""]
    if not survivors:
        lines.append("Никто. В бота не внедрять.")
    else:
        lines += [header, sep]
        for n in survivors:
            lines.append(fmt(df[(df["name"] == n) & (df["split"] == "OOS")].iloc[0].to_dict()))
            lines.append(f"  in-market OOS {100*in_mkt(nets[n][nets[n].index>=IS_END]):.0f}%")

    oos = df[df["split"] == "OOS"].sort_values("sharpe", ascending=False)
    lines += ["", "## OOS Sharpe", "", header, sep]
    for _, m in oos.iterrows():
        if "FEE20" in str(m["name"]):
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
