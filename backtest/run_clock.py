"""
run_clock.py — портфельный walk-forward направленных идей (НЕ арбитраж).

Отличие от run_pulse.py: считаем ОДИН портфель (веса → доходность книги),
а не сумму независимых сделок по 49 монетам.

Честность:
  * сигнал по close дня t, доходность с t+1 (лаг 1 день)
  * taker 10 bps one-way на |Δвеса|
  * IS до 2024-05-01, OOS после; параметры из литературы, не с OOS
  * нет заглядывания: rolling только backward

Запуск:
  .venv/bin/python backtest/run_clock.py --tag clock-v1
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest.engine import DATASET, load_symbol  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
IS_END = pd.Timestamp("2024-05-01", tz="UTC")
FEE_ONE_WAY = 0.0010  # 10 bps
MIN_HIST = 90

# Прекоммит вселенной «мажоры» (ликвид, не подгонка по OOS).
MAJORS = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "LINK", "DOT",
    "LTC", "ATOM", "NEAR", "UNI", "BCH", "TRX", "AAVE", "APT", "ARB", "SUI", "ICP",
}


def load_daily() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    closes, vols, funds = [], [], []
    for path in sorted((DATASET / "spot_1d").glob("*.csv.gz")):
        sym = path.name.removesuffix(".csv.gz")
        try:
            data = load_symbol(sym)
        except FileNotFoundError:
            continue
        spot = data["spot_1d"].copy()
        spot["dt"] = pd.to_datetime(spot["ts"], unit="ms", utc=True)
        spot = spot.set_index("dt").sort_index()
        closes.append(spot["close"].rename(sym))
        vols.append((spot["close"] * spot["volume"]).rename(sym))
        fr = data["funding"].copy()
        fr["dt"] = pd.to_datetime(fr["ts"], unit="ms", utc=True)
        fr = fr.set_index("dt").sort_index()["rate_pct"].rename(sym)
        funds.append(fr.resample("1D").last())
    close = pd.concat(closes, axis=1).sort_index()
    dollar_vol = pd.concat(vols, axis=1).sort_index().reindex(close.index)
    funding = pd.concat(funds, axis=1).sort_index().reindex(close.index).ffill()
    return close, dollar_vol, funding


def _sharpe(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 10 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * math.sqrt(365.0))


def _maxdd(r: pd.Series) -> float:
    eq = (1.0 + r.fillna(0.0)).cumprod()
    peak = eq.cummax()
    dd = eq / peak - 1.0
    return float(dd.min() * 100.0)


def _ann(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 5:
        return 0.0
    eq = float((1.0 + r).prod())
    years = len(r) / 365.0
    if years <= 0 or eq <= 0:
        return 0.0
    return (eq ** (1.0 / years) - 1.0) * 100.0


def _total(r: pd.Series) -> float:
    return float(((1.0 + r.fillna(0.0)).prod() - 1.0) * 100.0)


def _pf(r: pd.Series) -> float:
    pos = r[r > 0].sum()
    neg = abs(r[r < 0].sum())
    if neg <= 0:
        return math.inf if pos > 0 else 0.0
    return float(pos / neg)


def metrics(name: str, split: str, r: pd.Series, turnover: pd.Series) -> dict:
    r = r.dropna()
    wr = 100.0 * float((r > 0).mean()) if len(r) else 0.0
    return {
        "name": name,
        "split": split,
        "days": int(len(r)),
        "total_pct": round(_total(r), 2),
        "ann_pct": round(_ann(r), 2),
        "sharpe": round(_sharpe(r), 2),
        "maxdd_pct": round(_maxdd(r), 2),
        "pf": round(_pf(r), 2) if math.isfinite(_pf(r)) else 99.0,
        "winrate_d": round(wr, 1),
        "avg_to": round(float(turnover.reindex(r.index).fillna(0).mean()), 4),
    }


def apply_weights(w: pd.DataFrame, rets: pd.DataFrame, fee: float) -> tuple[pd.Series, pd.Series]:
    """w_t (известны на close t) зарабатывают rets_{t+1}; комиссия на |Δw|."""
    w = w.fillna(0.0)
    w = w.div(w.abs().sum(axis=1).replace(0.0, np.nan), axis=0).fillna(0.0)
    lagged = w.shift(1)
    gross = (lagged * rets).sum(axis=1)
    to = (w - w.shift(1)).abs().sum(axis=1).fillna(0.0)
    net = gross - fee * to
    return net.fillna(0.0), to


def vol_target(r: pd.Series, target: float = 0.15, look: int = 20, cap: float = 2.0) -> pd.Series:
    vol = r.rolling(look, min_periods=10).std() * math.sqrt(365.0)
    scale = (target / vol.replace(0.0, np.nan)).clip(0.0, cap)
    return (r * scale.shift(1)).fillna(0.0)


def scores(close: pd.DataFrame, funding: pd.DataFrame) -> dict[str, pd.DataFrame]:
    r1 = close.pct_change()
    out = {
        "ret7": close.pct_change(7),
        "ret14": close.pct_change(14),
        "ret28": close.pct_change(28),
        "ret30": close.pct_change(30),
        "ret90": close.pct_change(90),
        "ret180": close.pct_change(180),
        "vol14": r1.rolling(14).std(),
        "vol30": r1.rolling(30).std(),
        "sma50": close.rolling(50).mean(),
        "sma50_prev": close.rolling(50).mean().shift(5),
        "hist": close.expanding().count(),
        "fz": (
            (funding - funding.rolling(90, min_periods=20).mean())
            / funding.rolling(90, min_periods=20).std().replace(0.0, np.nan)
        ),
    }
    radj = (
        0.50 * (out["ret14"] / out["vol14"].replace(0.0, np.nan))
        + 0.30 * (out["ret30"] / out["vol30"].replace(0.0, np.nan))
        + 0.20 * (out["ret90"] / (r1.rolling(90).std().replace(0.0, np.nan)))
    )
    out["radj"] = radj
    out["above50"] = (close > out["sma50"]) & (out["sma50"] > out["sma50_prev"])
    return out


def xs_long_short(score: pd.DataFrame, mask: pd.DataFrame, q: float = 0.2) -> pd.DataFrame:
    s = score.where(mask)
    rk = s.rank(axis=1, pct=True)
    w = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    w = w.mask(rk >= 1.0 - q, 1.0)
    w = w.mask(rk <= q, -1.0)
    return w


def xs_long_only(score: pd.DataFrame, mask: pd.DataFrame, q: float = 0.2) -> pd.DataFrame:
    s = score.where(mask)
    rk = s.rank(axis=1, pct=True)
    w = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    w = w.mask(rk >= 1.0 - q, 1.0)
    return w


def topk(score: pd.DataFrame, mask: pd.DataFrame, k: int = 4) -> pd.DataFrame:
    s = score.where(mask)
    w = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    # nlargest per row
    arr = s.to_numpy()
    cols = list(s.columns)
    for i, row in enumerate(arr):
        valid = np.where(np.isfinite(row))[0]
        if len(valid) == 0:
            continue
        take = valid[np.argsort(row[valid])[-min(k, len(valid)):]]
        for j in take:
            w.iloc[i, j] = 1.0
    return w


def breadth_gate(above: pd.DataFrame, mask: pd.DataFrame) -> pd.Series:
    n = mask.sum(axis=1).replace(0.0, np.nan)
    b = (above & mask).sum(axis=1) / n
    g = pd.Series(0.0, index=above.index)
    g = g.mask(b >= 0.30, 0.30)
    g = g.mask(b >= 0.50, 0.60)
    g = g.mask(b >= 0.70, 1.00)
    return g.fillna(0.0)


def weekly_hold(w: pd.DataFrame, every: int = 7) -> pd.DataFrame:
    """Веса считаются каждый день, но ребаланс раз в `every` дней (меньше издержек)."""
    idx = np.arange(len(w))
    keep = (idx % every) == 0
    held = w.copy()
    held.loc[~keep] = np.nan
    return held.ffill().fillna(0.0)


def build_strats(close: pd.DataFrame, sc: dict, universe: list[str]) -> dict[str, pd.DataFrame]:
    c = close[universe]
    mask = sc["hist"][universe] >= MIN_HIST
    above = sc["above50"][universe].fillna(False)
    fz = sc["fz"][universe]
    strats: dict[str, pd.DataFrame] = {}

    strats["EW_BAH"] = weekly_hold(mask.astype(float))
    btc = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    if "BTC" in c.columns:
        btc["BTC"] = 1.0
    strats["BTC_BAH"] = btc

    ts = np.sign(sc["ret14"][universe]).where(mask, 0.0)
    strats["TSMOM_14"] = weekly_hold(ts.fillna(0.0))

    strats["CSMOM_14_LS"] = weekly_hold(xs_long_short(sc["ret14"][universe], mask, 0.2))
    strats["CSMOM_7_LS"] = weekly_hold(xs_long_short(sc["ret7"][universe], mask, 0.2))
    strats["CSMOM_28_LS"] = weekly_hold(xs_long_short(sc["ret28"][universe], mask, 0.2))
    strats["CSMOM_14_LO"] = weekly_hold(xs_long_only(sc["ret14"][universe], mask, 0.2))

    strats["MA50_LO"] = weekly_hold(above.astype(float).where(mask, 0.0))

    # funding fade weekly
    strats["FUND_FADE_LS"] = weekly_hold(xs_long_short(-fz, mask, 0.2))

    # literature: risk-adj mom top4 + trend + breadth (long-only)
    pick_mask = mask & above
    top = topk(sc["radj"][universe], pick_mask, k=4)
    gate = breadth_gate(above, mask)
    b4 = weekly_hold(top)
    strats["BREADTH_TOP4"] = b4.mul(gate, axis=0)

    # OWN CLOCK: breadth top4 + не лонгить перегретый funding + чуть уже вход
    clock_mask = pick_mask & (fz.fillna(0.0) < 1.5)
    clock_top = topk(sc["radj"][universe], clock_mask, k=4)
    strats["CLOCK"] = weekly_hold(clock_top).mul(gate, axis=0)

    # OWN CLOCK_LS: лонг топ radj, шорт низ radj, только тренд-фильтр на лонге,
    # шорт только если ниже SMA50 и funding не слишком отрицательный (не squeeze).
    lo = xs_long_only(sc["radj"][universe], pick_mask, 0.25)
    sh_mask = mask & (~above) & (fz.fillna(0.0) > -1.5)
    sh = xs_long_only(-sc["radj"][universe], sh_mask, 0.25) * -1.0
    strats["CLOCK_LS"] = weekly_hold(lo + sh)

    return strats


def split_rets(r: pd.Series, to: pd.Series) -> dict[str, tuple[pd.Series, pd.Series]]:
    is_mask = r.index < IS_END
    return {
        "IS": (r[is_mask], to[is_mask]),
        "OOS": (r[~is_mask], to[~is_mask]),
        "ALL": (r, to),
    }


def run(tag: str) -> int:
    print("Загрузка дневной панели...")
    close, dollar_vol, funding = load_daily()
    print(f"  {close.shape[1]} монет, {len(close)} дней, {close.index.min().date()} → {close.index.max().date()}")
    sc = scores(close, funding)
    rets = close.pct_change()

    universes = {
        "ALL": [c for c in close.columns],
        "MAJ": [c for c in close.columns if c in MAJORS],
    }

    rows = []
    curves = {}
    for uname, cols in universes.items():
        print(f"Вселенная {uname} ({len(cols)})...")
        strats = build_strats(close, sc, cols)
        sub_rets = rets[cols]
        for sname, w in strats.items():
            w = w.reindex(close.index).reindex(columns=cols).fillna(0.0)
            net, to = apply_weights(w, sub_rets, FEE_ONE_WAY)
            full_name = f"{uname}:{sname}"
            curves[full_name] = net
            # vol-targeted twin for LS / CLOCK
            if sname in ("CSMOM_14_LS", "CLOCK", "CLOCK_LS", "BREADTH_TOP4", "TSMOM_14"):
                vt = vol_target(net, 0.15)
                curves[full_name + "_VT"] = vt
                # turnover same order of magnitude — approximate with original to
                for split, (rr, tt) in split_rets(vt, to).items():
                    rows.append(metrics(full_name + "_VT", split, rr, tt))
            for split, (rr, tt) in split_rets(net, to).items():
                rows.append(metrics(full_name, split, rr, tt))

    df = pd.DataFrame(rows)

    def fmt_row(m: dict) -> str:
        return (
            f"| {m['name']} | {m['split']} | {m['days']} | {m['total_pct']:+.1f} | "
            f"{m['ann_pct']:+.1f} | {m['sharpe']:.2f} | {m['maxdd_pct']:.1f} | "
            f"{m['pf']:.2f} | {m['winrate_d']:.1f} | {m['avg_to']:.3f} |"
        )

    header = (
        "| стратегия | сплит | дней | итог% | годовые% | Sharpe | maxDD% | PF | wr% дней | оборот |"
    )
    sep = "|" + "---|" * 10

    oos = df[df["split"] == "OOS"].copy()
    # порог портфеля: OOS Sharpe≥0.40, годовые>0, PF≥1.15, не хуже BTC по шарпу? мягче:
    # «есть живой edge» = Sharpe≥0.5 и ann>0 и maxDD лучше -60%
    survivors = oos[
        (oos["sharpe"] >= 0.50)
        & (oos["ann_pct"] > 0)
        & (oos["pf"] >= 1.15)
        & (~oos["name"].str.contains("BAH"))
    ].sort_values(["sharpe", "ann_pct"], ascending=False)

    lines = [
        "# Портфельный бектест CLOCK (направление, не арбитраж)",
        "",
        f"- Период: **{close.index.min().date()} → {close.index.max().date()}** · {close.shape[1]} монет",
        f"- Сплит: IS до {IS_END.date()} · OOS после",
        "- ТФ: 1д спот · сигнал close t → доходность t+1 · ребаланс раз в 7 дней",
        f"- Издержки: {FEE_ONE_WAY*100:.2f}% one-way на оборот весов",
        "- Вселенные: ALL (все 49) и MAJ (21 ликвидная, список прекоммит)",
        "- Параметры (литература, не OOS): formation 7/14/28д, top/bottom 20%, "
        "SMA50 breadth 0/30/60/100, vol-target 15% годовых",
        "",
        "## Сводка",
        "",
        header,
        sep,
    ]
    order = ["IS", "OOS", "ALL"]
    names = list(dict.fromkeys(df["name"]))
    for name in names:
        for split in order:
            sub = df[(df["name"] == name) & (df["split"] == split)]
            if sub.empty:
                continue
            lines.append(fmt_row(sub.iloc[0].to_dict()))

    lines += [
        "",
        "## Как читать",
        "",
        "- Это **портфель** (компаунд книги), не сумма сделок как в pulse-v1.",
        "- BTC_BAH / EW_BAH — обязательный бенчмарк.",
        "- Порог «есть edge»: OOS Sharpe ≥ 0.50, годовые > 0, PF ≥ 1.15.",
        "- `_VT` = Barroso vol-targeting 15% (масштаб по прошлой воле, лаг 1).",
        "",
        "## Кто выжил на OOS",
        "",
    ]
    if survivors.empty:
        lines.append(
            "Ни одна направленная портфельная система **не прошла** порог. "
            "В бота не внедрять."
        )
    else:
        lines += [header, sep]
        for _, m in survivors.iterrows():
            lines.append(fmt_row(m.to_dict()))
        lines.append("")
        lines.append(
            "Кандидаты есть на OOS. Это ещё не внедрение: нужна проверка "
            "устойчивости (другой weekday ребаланса, стресс комиссий)."
        )

    oos_sorted = oos.sort_values("sharpe", ascending=False)
    lines += ["", "## OOS: рейтинг по Sharpe", "", header, sep]
    for _, m in oos_sorted.iterrows():
        lines.append(fmt_row(m.to_dict()))

    # vs BTC
    btc_oos = oos[oos["name"] == "MAJ:BTC_BAH"]
    btc_sh = float(btc_oos["sharpe"].iloc[0]) if len(btc_oos) else 0.0
    btc_ann = float(btc_oos["ann_pct"].iloc[0]) if len(btc_oos) else 0.0
    lines += [
        "",
        "## Выводы",
        "",
        f"BTC buy&hold OOS: {btc_ann:+.1f}% годовых, Sharpe {btc_sh:.2f}.",
        "CLOCK — собственный композит: risk-adj momentum (14/30/90) + SMA50 "
        "тренд + breadth-гейт + veto экстремального funding.",
        "Арбитраж v3 по-прежнему не заменяется этим слоем, пока OOS-порог не взят.",
        "",
    ]

    report = "\n".join(lines) + "\n"
    print(report)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{tag}.md").write_text(report, encoding="utf-8")
    payload = {"tag": tag, "rows": rows, "survivors": survivors["name"].tolist() if len(survivors) else []}
    (RESULTS / f"{tag}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Сохранено backtest/results/{tag}.md")
    return 0 if len(survivors) else 2


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tag", default="clock-v1")
    return run(p.parse_args().tag)


if __name__ == "__main__":
    raise SystemExit(main())
