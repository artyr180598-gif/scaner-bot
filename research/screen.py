"""
research/screen.py — скрининг признаков: что реально предсказывает исход сделки.

Метод: делим выборку по времени на TRAIN (обучение/поиск) и TEST (честный
out-of-sample). Для каждого признака смотрим средний результат в R по
децилям. Признак полезен, только если эффект СОХРАНЯЕТСЯ на TEST.
"""
from __future__ import annotations

import sys
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "/home/user/scaner-bot")

SPLIT = pd.Timestamp("2024-07-01", tz="UTC")

SKIP = {"ts", "coin", "open", "high", "low", "close", "volume", "timestamp",
        "date", "atr", "ema20", "ema50", "ema200", "R_long", "R_short",
        "funding_rate", "dollar_vol"}


def decile_table(df: pd.DataFrame, feat: str, target: str, q: int = 10) -> pd.DataFrame:
    sub = df[[feat, target]].dropna()
    if len(sub) < 5000:
        return pd.DataFrame()
    try:
        b = pd.qcut(sub[feat], q, labels=False, duplicates="drop")
    except Exception:
        return pd.DataFrame()
    g = sub.groupby(b)[target].agg(["mean", "count"])
    return g


def spread_score(df: pd.DataFrame, feat: str, target: str) -> tuple[float, float]:
    """Возвращает (эффект = R верхнего дециля − R нижнего, корреляция ранга)."""
    t = decile_table(df, feat, target)
    if t.empty or len(t) < 5:
        return np.nan, np.nan
    eff = float(t["mean"].iloc[-1] - t["mean"].iloc[0])
    x = np.arange(len(t))
    ic = float(np.corrcoef(x, t["mean"].to_numpy())[0, 1])
    return eff, ic


def main():
    df = pd.read_pickle("research/cache/perp4h_feat.pkl")
    df = df[df["dollar_vol"] > 3e5]          # только ликвидные бары
    tr = df[df.ts < SPLIT]
    te = df[df.ts >= SPLIT]
    print(f"TRAIN {len(tr):,} баров ({tr.ts.min().date()}→{tr.ts.max().date()}), "
          f"TEST {len(te):,} ({te.ts.min().date()}→{te.ts.max().date()})")
    print(f"базовая линия TRAIN: long {tr.R_long.mean():+.4f}R  short {tr.R_short.mean():+.4f}R")
    print(f"базовая линия TEST : long {te.R_long.mean():+.4f}R  short {te.R_short.mean():+.4f}R\n")

    feats = [c for c in df.columns if c not in SKIP and df[c].dtype.kind in "fi"]
    rows = []
    for f in feats:
        for side in ("R_long", "R_short"):
            e_tr, ic_tr = spread_score(tr, f, side)
            e_te, ic_te = spread_score(te, f, side)
            rows.append(dict(feat=f, side=side[2:], eff_tr=e_tr, ic_tr=ic_tr,
                             eff_te=e_te, ic_te=ic_te))
    r = pd.DataFrame(rows).dropna()
    # устойчивые: эффект одного знака и заметный на обеих половинах
    r["stable"] = (np.sign(r.eff_tr) == np.sign(r.eff_te)) & \
                  (r.eff_tr.abs() > 0.03) & (r.eff_te.abs() > 0.03)
    r["score"] = np.where(r.stable, np.minimum(r.eff_tr.abs(), r.eff_te.abs()), 0)
    r = r.sort_values("score", ascending=False)
    pd.set_option("display.width", 200)
    print("=== УСТОЙЧИВЫЕ ПРЕДИКТОРЫ (эффект сохранился вне обучающей выборки) ===")
    print(r[r.stable].head(30).to_string(index=False,
          formatters={c: "{:+.3f}".format for c in ("eff_tr", "ic_tr", "eff_te", "ic_te")}))
    print("\n=== РАЗВАЛИЛИСЬ НА TEST (обучающая иллюзия) ===")
    bad = r[(~r.stable) & (r.eff_tr.abs() > 0.08)].head(15)
    print(bad.to_string(index=False,
          formatters={c: "{:+.3f}".format for c in ("eff_tr", "ic_tr", "eff_te", "ic_te")}))
    r.to_csv("research/cache/screen_perp4h.csv", index=False)


if __name__ == "__main__":
    main()
