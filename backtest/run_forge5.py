"""
forge-v5 — качество ВХОДА на LOW_CHAN, без vol-target и без кэша.

Правила те же: overlapping 7д, PIT топ-12, лаг 1, вес ≤15%,
IS до 2024-05-01, fee 10 bps + стресс 20, in-market ≥40%.

Идея: LOW_CHAN уже живой picker, но покупает хаи. Литература
(Donchian 20, pullback к SMA50 в тренде, persist) — фильтры входа,
не масштаб риска.
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
    topk,
)
from backtest.run_forge import btc_beta_resid, cap_weights, overlapping, pit_mask  # noqa: E402
from backtest.run_forge2 import MID, halves  # noqa: E402

IN_MKT_MIN = 0.40


def in_mkt(r: pd.Series) -> float:
    return float((r.abs() > 1e-12).mean())


def chandelier_ok(close: pd.DataFrame, k: float = 2.5) -> pd.DataFrame:
    atr = close.pct_change().abs().rolling(14, min_periods=5).mean() * close
    hh = close.rolling(20, min_periods=5).max()
    return close >= (hh - k * atr)


def donchian_break(close: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Новый n-дневный хай: close ≥ вчерашний rolling-max."""
    hh_prev = close.rolling(n, min_periods=5).max().shift(1)
    return close >= hh_prev


def entry_stats(w: pd.DataFrame, rets: pd.DataFrame, horizon: int = 7) -> dict:
    """Когда вес имени вспыхивает 0→>0, средний форвард h дней (до издержек)."""
    prev = w.shift(1).fillna(0.0)
    fire = (w > 0) & (prev <= 0)
    fwd = rets.shift(-1).rolling(horizon).sum().shift(-(horizon - 1))
    # rolling sum of next h days: use loop-free
    acc = None
    for i in range(1, horizon + 1):
        piece = rets.shift(-i)
        acc = piece if acc is None else acc + piece
    samples = acc.where(fire)
    flat = samples.stack()
    flat = flat.replace([np.inf, -np.inf], np.nan).dropna()
    if flat.empty:
        return {"n": 0, "avg": 0.0, "wr": 0.0, "med": 0.0}
    return {
        "n": int(len(flat)),
        "avg": round(float(flat.mean()) * 100.0, 3),
        "wr": round(float((flat > 0).mean()) * 100.0, 1),
        "med": round(float(flat.median()) * 100.0, 3),
    }


def persist(mask: pd.DataFrame, n: int = 3, win: int = 5) -> pd.DataFrame:
    return mask.astype(int).rolling(win, min_periods=n).sum() >= n


def run(tag: str = "forge-v5") -> int:
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
    chan = chandelier_ok(close, 2.5)
    brk = donchian_break(close, 20)
    hh = close.rolling(20, min_periods=5).max()
    sma = sc["sma50"]
    atr = close.pct_change().abs().rolling(14, min_periods=5).mean() * close
    ext = (close - sma) / atr.replace(0.0, np.nan)
    chase = close.pct_change(5)
    pull = (close <= hh * 0.92) & (close >= sma)

    base_pick = topk(resid, quiet, 4)
    low_eq = base_pick.mul(g, axis=0)
    low_chan = low_eq.where(chan, 0.0)

    # --- входы (фильтр на pick, выход chandelier) ---
    def hold(picks: pd.DataFrame) -> pd.DataFrame:
        return picks.mul(g, axis=0).where(chan, 0.0)

    rs_pos = quiet & (resid > 0)
    pick_pos = topk(resid, rs_pos, 4)

    raw: dict[str, pd.DataFrame] = {
        "LOW_CHAN": low_chan,
        # подтверждение Дончиана 20 (классический turtle-вход)
        "CHAN_BRK20": hold(topk(resid, quiet & brk, 4)),
        # отбой: тихий победитель, 8%+ от хая, всё ещё выше SMA50
        "CHAN_PULL": hold(topk(resid, quiet & pull, 4)),
        # persist: 3 из 5 дней в топ-тихих
        "CHAN_PERS": hold(base_pick.where(persist(base_pick > 0, 3, 5), 0.0)),
        # остаток строго > 0 (не «лучший из слабых»)
        "CHAN_RSPOS": hold(pick_pos),
        # не гнаться: 5д < 15%
        "CHAN_NOCHASE": hold(topk(resid, quiet & (chase.fillna(0) < 0.15), 4)),
        # не перерастянут: (close-SMA)/ATR < 2
        "CHAN_NOEXT": hold(topk(resid, quiet & (ext.fillna(99) < 2.0), 4)),
        # топ-2 вместо 4 — более жёсткий сигнал
        "CHAN_TOP2": topk(resid, quiet, 2).mul(g, axis=0).where(chan, 0.0),
        # Donchian среди тихих, без residual rank
        "DONCH_Q": (quiet & brk).astype(float).mul(g, axis=0).where(chan, 0.0),
        # вход: persist + residual>0 + не chase (комбо литературы)
        "CHAN_QENTRY": hold(
            topk(
                resid,
                quiet
                & (resid > 0)
                & persist(base_pick > 0, 2, 4)
                & (chase.fillna(0) < 0.20),
                4,
            )
        ),
        # отбой ИЛИ свежий breakout (два типа входа)
        "CHAN_BRK_OR_PULL": hold(topk(resid, quiet & (brk | pull), 4)),
        # вход только в день переворота chandelier False→True
        "CHAN_FLIP": hold(base_pick.where(chan & (~chan.shift(1).fillna(False)), 0.0)),
    }
    # FLIP as written zeros next days — that's cash. Hold after flip:
    flip = chan & (~chan.shift(1).fillna(False))
    entered = (base_pick > 0) & flip
    # once in, hold while chandelier and still quiet-ish: use ffill of pick until chan dies
    hold_on = pd.DataFrame(0.0, index=close.index, columns=cols)
    state = np.zeros(len(cols))
    bp = base_pick.fillna(0.0).to_numpy()
    fl = flip.fillna(False).to_numpy()
    ch = chan.fillna(False).to_numpy()
    for i in range(len(close)):
        # new entries
        state = np.where(fl[i] & (bp[i] > 0), 1.0, state)
        # exit
        state = np.where(ch[i], state, 0.0)
        hold_on.iloc[i] = state
    raw["CHAN_FLIPHOLD"] = hold_on.mul(g, axis=0)

    rows: list[dict] = []
    nets: dict[str, pd.Series] = {}
    tos: dict[str, pd.Series] = {}
    entries: dict[str, dict] = {}

    def sim(w: pd.DataFrame, fee: float = FEE_ONE_WAY):
        ww = cap_weights(
            overlapping(w.fillna(0.0).reindex(close.index).reindex(columns=cols).fillna(0.0))
        )
        return apply_weights(ww, rets, fee), ww

    for name, w in raw.items():
        print(" ", name)
        (net, to), ww = sim(w)
        nets[name] = net
        tos[name] = to
        rows.extend(halves(net, to, name))
        net20, to20 = sim(w, 0.002)[0]
        rows.extend(halves(net20, to20, name + "_FEE20"))
        oos_w = ww[ww.index >= IS_END]
        oos_r = rets[rets.index >= IS_END]
        entries[name] = entry_stats(oos_w, oos_r, 7)
        entries[name]["in_mkt"] = round(100.0 * in_mkt(net[net.index >= IS_END]), 1)

    def pass_bar(name: str) -> bool:
        by = {(r["name"], r["split"]): r for r in rows}
        o, i = by.get((name, "OOS")), by.get((name, "IS"))
        a, b = by.get((name, "OOS1")), by.get((name, "OOS2"))
        if not all((o, i, a, b)):
            return False
        if o["sharpe"] < 0.55 or o["ann_pct"] < 8 or o["pf"] < 1.15:
            return False
        if i["ann_pct"] <= 0:
            return False
        if a["ann_pct"] <= 0 or b["ann_pct"] <= 0:
            return False
        if a["pf"] < 1.15:
            return False
        if in_mkt(nets[name][nets[name].index >= IS_END]) < IN_MKT_MIN:
            return False
        return True

    def fmt(m):
        return (
            f"| {m['name']} | {m['split']} | {m['days']} | {m['total_pct']:+.1f} | "
            f"{m['ann_pct']:+.1f} | {m['sharpe']:.2f} | {m['maxdd_pct']:.1f} | {m['pf']:.2f} |"
        )

    header = "| стратегия | сплит | дней | итог% | год% | Sharpe | maxDD% | PF |"
    sep = "|" + "---|" * 8
    df = pd.DataFrame(rows)
    cands = [n for n in raw if n in nets]
    survivors = [n for n in cands if pass_bar(n)]

    lines = [
        "# FORGE v5 — фильтры ВХОДА на LOW_CHAN (без VT/кэша)",
        "",
        "Порог: OOS Sharpe≥0.55 год≥8% PF≥1.15, обе половины >0, **OOS1 PF≥1.15**, in-market ≥40%.",
        "DD −22% не требуем (честный потолок v4). Цель — лучший вход, не меньше ямы любой ценой.",
        "",
        header,
        sep,
    ]
    for n in cands:
        for sp in ("IS", "OOS", "OOS1", "OOS2"):
            sub = df[(df["name"] == n) & (df["split"] == sp)]
            if len(sub):
                lines.append(fmt(sub.iloc[0].to_dict()))

    lines += ["", "## Входы OOS (вес 0→>0, форвард 7д до комиссий)", ""]
    lines += ["| стратегия | n входов | wr% | avg% | med% | in-mkt% |", "|---|---:|---:|---:|---:|---:|"]
    for n in cands:
        e = entries[n]
        lines.append(
            f"| {n} | {e['n']} | {e['wr']:.1f} | {e['avg']:+.3f} | {e['med']:+.3f} | {e['in_mkt']:.0f} |"
        )

    lines += ["", "## Прошли порог (живой вход, не кэш)", ""]
    if not survivors:
        lines.append("Никто кроме, возможно, базы. В авто-вход не внедрять.")
    else:
        lines += [header, sep]
        for n in survivors:
            lines.append(fmt(df[(df["name"] == n) & (df["split"] == "OOS")].iloc[0].to_dict()))
            e = entries[n]
            lines.append(
                f"  in-mkt {e['in_mkt']:.0f}% · OOS-входы n={e['n']} wr={e['wr']}% avg7д={e['avg']:+.2f}%"
            )

    oos = df[df["split"] == "OOS"].copy()
    oos = oos[~oos["name"].str.contains("FEE20")].sort_values("sharpe", ascending=False)
    lines += ["", "## OOS Sharpe", "", header, sep]
    for _, m in oos.iterrows():
        lines.append(fmt(m.to_dict()))

    # monthly OOS1 of baseline for diagnosis
    base = nets["LOW_CHAN"]
    o1 = base[(base.index >= IS_END) & (base.index < MID)]
    monthly = (1.0 + o1).resample("ME").prod() - 1.0
    lines += ["", "## LOW_CHAN OOS1 по месяцам", ""]
    for dt, v in monthly.items():
        lines.append(f"- {dt.date()}: {100*v:+.1f}%")

    report = "\n".join(lines) + "\n"
    print(report)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{tag}.md").write_text(report, encoding="utf-8")
    (RESULTS / f"{tag}.json").write_text(
        json.dumps(
            {"survivors": survivors, "rows": rows, "entries": entries},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print("survivors:", survivors)
    return 0 if survivors else 2


if __name__ == "__main__":
    raise SystemExit(run())
