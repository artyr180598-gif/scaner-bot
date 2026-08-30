"""
run_forge.py — итерация направленных портфелей.

Правила (иначе результат не считается):
  * overlapping 7д (нет любимого weekday)
  * вселенная point-in-time: топ-N по 30д dollar volume
  * сигнал close t → доходность t+1
  * вес имени ≤ 15%
  * день с |r|>40% = брак (взорванный шорт)
  * OOS после 2024-05-01; параметры не крутим по OOS
  * стресс комиссии 20 bps обязателен

Запуск: .venv/bin/python backtest/run_forge.py --tag forge-v1
"""
from __future__ import annotations

import argparse
import json
import math
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
    vol_target,
    xs_long_only,
)

MIN_HIST = 90
TOP_N = 12
WCAP = 0.15
MAX_DAY = 0.40


def pit_mask(dollar_vol: pd.DataFrame, hist: pd.DataFrame, n: int = TOP_N) -> pd.DataFrame:
    adv = dollar_vol.rolling(30, min_periods=10).mean()
    ok = hist >= MIN_HIST
    ranked = adv.where(ok).rank(axis=1, ascending=False, method="first")
    return (ranked <= n) & ok


def cap_weights(w: pd.DataFrame, cap: float = WCAP) -> pd.DataFrame:
    w = w.fillna(0.0).clip(-cap, cap)
    return w


def overlapping(w: pd.DataFrame, n: int = 7) -> pd.DataFrame:
    return w.rolling(n, min_periods=1).mean()


def eval_w(name: str, w: pd.DataFrame, rets: pd.DataFrame, fee: float = FEE_ONE_WAY) -> dict:
    w = cap_weights(overlapping(w.fillna(0.0)))
    net, to = apply_weights(w, rets, fee)
    # twin vol-target 12%
    vt = vol_target(net, 0.12, cap=1.5)
    out = {"name": name, "net": net, "vt": vt, "to": to}
    out["bad"] = bool((net.abs() > MAX_DAY).any())
    return out


def pack(name: str, net: pd.Series, to: pd.Series) -> list[dict]:
    rows = []
    for split, (rr, tt) in split_rets(net, to).items():
        m = metrics(name, split, rr, tt)
        m["min_d"] = round(float(rr.min()) if len(rr) else 0.0, 3)
        m["max_d"] = round(float(rr.max()) if len(rr) else 0.0, 3)
        rows.append(m)
    return rows


def btc_beta_resid(rets: pd.DataFrame, win: int = 60) -> pd.DataFrame:
    btc = rets["BTC"]
    var = btc.rolling(win, min_periods=30).var()
    betas = pd.DataFrame(index=rets.index, columns=rets.columns, dtype=float)
    for col in rets.columns:
        cov = rets[col].rolling(win, min_periods=30).cov(btc)
        betas[col] = cov / var.replace(0.0, np.nan)
    resid = rets.sub(betas.mul(btc, axis=0), axis=0)
    return (1.0 + resid.fillna(0.0)).rolling(14, min_periods=7).apply(
        lambda x: np.prod(x) - 1.0, raw=True
    )


def run(tag: str) -> int:
    print("Загрузка...")
    close, dvol, funding = load_daily()
    sc = scores(close, funding)
    rets = close.pct_change()
    cols = list(close.columns)
    pit = pit_mask(dvol, sc["hist"])
    above = sc["above50"].fillna(False)
    fz = sc["fz"]
    radj = sc["radj"]
    btc_up = (close["BTC"] > sc["sma50"]["BTC"]).fillna(False)
    btc_up100 = (close["BTC"] > close["BTC"].rolling(100).mean()).fillna(False)
    abs90 = sc["ret90"]["BTC"] > 0

    # residual mom (vs BTC)
    print("  residual momentum...")
    resid14 = btc_beta_resid(rets)

    def gated(score: pd.DataFrame, extra: pd.DataFrame | None = None) -> pd.DataFrame:
        m = pit & above
        if extra is not None:
            m = m & extra
        return topk(score, m, k=4)

    strats: dict[str, pd.DataFrame] = {}

    # --- бенчмарки ---
    btc = pd.DataFrame(0.0, index=close.index, columns=cols)
    btc["BTC"] = 1.0
    strats["BTC_BAH"] = btc
    strats["PIT_EW"] = pit.astype(float)

    # Dual momentum BTC: long BTC if 90d > 0 else cash
    dm = btc.copy()
    dm.loc[~abs90, "BTC"] = 0.0
    strats["DM_BTC_CASH"] = dm

    # Dual BTC vs ETH
    dm2 = pd.DataFrame(0.0, index=close.index, columns=cols)
    stronger = np.where(sc["ret90"]["BTC"] >= sc["ret90"]["ETH"], "BTC", "ETH")
    for i, asset in enumerate(stronger):
        if not np.isfinite(sc["ret90"].iloc[i][asset]):
            continue
        if sc["ret90"].iloc[i][asset] > 0:
            dm2.iloc[i, dm2.columns.get_loc(asset)] = 1.0
    strats["DM_BTC_ETH"] = dm2

    # BTC SMA100 long/cash
    sma = btc.copy()
    sma.loc[~btc_up100, "BTC"] = 0.0
    strats["BTC_SMA100"] = sma

    # TSMOM sign 90d vol-later
    ts = np.sign(sc["ret90"]).where(pit, 0.0)
    strats["TSMOM_90_PIT"] = ts.fillna(0.0)

    # Cross-section 14d long-only PIT
    strats["CS14_LO_PIT"] = xs_long_only(sc["ret14"], pit, 0.25).fillna(0.0)

    # Breadth top4 PIT
    g = breadth_gate(above, pit)
    strats["BREADTH4"] = gated(radj).mul(g, axis=0)

    # CLOCK: radj + trend + funding veto + breadth, PIT
    clock_extra = fz.fillna(0.0) < 1.2
    strats["CLOCK"] = gated(radj, clock_extra).mul(g, axis=0)

    # FORGE: CLOCK only when BTC regime on (SMA100), else cash
    forge = gated(radj, clock_extra).mul(g, axis=0)
    forge = forge.mul(btc_up100.astype(float), axis=0)
    strats["FORGE"] = forge

    # FORGE_SOFT: 30% BTC when regime off, CLOCK when on
    soft = forge.copy()
    off = ~btc_up100
    soft.loc[off, :] = 0.0
    soft.loc[off, "BTC"] = 1.0
    strats["FORGE_SOFT"] = soft

    # Residual momentum top4 among PIT+trend
    strats["RESID4"] = gated(resid14).mul(g, axis=0)

    # Double sort: high radj AND low funding_z
    combo = radj - 0.35 * fz.fillna(0.0)
    strats["MOM_FUND"] = gated(combo).mul(g, axis=0)

    # Inverse-vol of PIT names above SMA50
    inv = (1.0 / sc["vol30"].replace(0.0, np.nan)).where(pit & above, 0.0)
    strats["INVVOL"] = inv.fillna(0.0)

    # Long PIT winners, NO shorts, skip crowded
    strats["WINNERS"] = xs_long_only(radj, pit & above & (fz.fillna(0.0) < 1.0), 0.3).fillna(0.0)

    print(f"Стратегий: {len(strats)}")
    rows: list[dict] = []
    curves = {}
    for name, w in strats.items():
        print(f"  {name}...")
        w = w.reindex(close.index).reindex(columns=cols).fillna(0.0)
        pack_ = eval_w(name, w, rets)
        if pack_["bad"] and name not in ("BTC_BAH", "PIT_EW"):
            print(f"    BRACK |r|>40% — отброшено")
            continue
        curves[name] = pack_["net"]
        rows.extend(pack(name, pack_["net"], pack_["to"]))
        rows.extend(pack(name + "_VT", pack_["vt"], pack_["to"]))
        # fee stress 20 bps on raw (not vt) for survivors later
        net20, to20 = apply_weights(cap_weights(overlapping(w)), rets, 0.002)
        rows.extend(pack(name + "_FEE20", net20, to20))

    df = pd.DataFrame(rows)

    def fmt(m: dict) -> str:
        return (
            f"| {m['name']} | {m['split']} | {m.get('days', 0)} | {m['total_pct']:+.1f} | "
            f"{m['ann_pct']:+.1f} | {m['sharpe']:.2f} | {m['maxdd_pct']:.1f} | "
            f"{m['pf']:.2f} | {m.get('min_d', 0):.2f} | {m.get('max_d', 0):.2f} |"
        )

    header = "| стратегия | сплит | дней | итог% | год% | Sharpe | maxDD% | PF | minD | maxD |"
    sep = "|" + "---|" * 10

    oos = df[df["split"] == "OOS"]
    is_ = df[df["split"] == "IS"]

    def is_ok(name: str) -> bool:
        if "_FEE20" in name:
            return False
        o = oos[oos["name"] == name]
        i = is_[is_["name"] == name]
        f = oos[oos["name"] == name.replace("_VT", "") + "_FEE20"]
        if o.empty or i.empty:
            return False
        o, i = o.iloc[0], i.iloc[0]
        if o["sharpe"] < 0.50 or o["ann_pct"] <= 0 or o["pf"] < 1.15:
            return False
        if o["maxdd_pct"] < -40.0:
            return False
        if i["sharpe"] < 0.20 or i["ann_pct"] <= 0:
            return False
        if not f.empty and f.iloc[0]["sharpe"] < 0.30:
            return False
        if abs(o.get("max_d", 0)) > 0.35 or abs(o.get("min_d", 0)) > 0.35:
            return False
        return True

    names = [n for n in df["name"].unique() if not n.endswith("_FEE20")]
    survivors = [n for n in names if is_ok(n)]

    lines = [
        "# FORGE — направленные портфели (overlapping, PIT-ликвидность)",
        "",
        f"- Период {close.index.min().date()} → {close.index.max().date()}",
        f"- IS до {IS_END.date()}, OOS после. Overlapping 7д. PIT топ-{TOP_N} по 30д объёму.",
        f"- Вес ≤ {WCAP:.0%}. Fee 10 bps (стресс 20). Лаг 1 день.",
        "- Порог: OOS Sharpe≥0.50, год>0, PF≥1.15, DD лучше −40%, IS не отрицательный, fee20 Sharpe≥0.30.",
        "",
        "## Все прогоны",
        "",
        header,
        sep,
    ]
    for name in names:
        for split in ("IS", "OOS"):
            sub = df[(df["name"] == name) & (df["split"] == split)]
            if len(sub):
                lines.append(fmt(sub.iloc[0].to_dict()))

    lines += ["", "## Кто прошёл ВСЕ фильтры (включая fee20 и DD)", ""]
    if not survivors:
        lines.append("Никто. В бота не внедрять.")
    else:
        lines += [header, sep]
        for n in survivors:
            sub = df[(df["name"] == n) & (df["split"] == "OOS")].iloc[0]
            lines.append(fmt(sub.to_dict()))

    oos_plain = oos[~oos["name"].str.contains("FEE20")].sort_values("sharpe", ascending=False)
    lines += ["", "## OOS по Sharpe (без FEE20)", "", header, sep]
    for _, m in oos_plain.iterrows():
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
    p = argparse.ArgumentParser()
    p.add_argument("--tag", default="forge-v1")
    raise SystemExit(run(p.parse_args().tag))
