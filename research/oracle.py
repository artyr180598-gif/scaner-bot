"""
research/oracle.py — «потолок предсказуемости».

Обучаем градиентный бустинг на TRAIN и честно проверяем на TEST.
Цель — не выкатить ML в бота, а понять: есть ли вообще в этих признаках
предсказуемый сигнал и какие из них несут информацию. Потом самые
устойчивые превращаем в простые правила, которые можно объяснить словами.
"""
from __future__ import annotations

import sys
import warnings
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

warnings.filterwarnings("ignore")
sys.path.insert(0, "/home/user/scaner-bot")

SPLIT = pd.Timestamp("2024-07-01", tz="UTC")
SKIP = {"ts", "coin", "open", "high", "low", "close", "volume", "timestamp",
        "date", "atr", "ema20", "ema50", "ema200", "R_long", "R_short",
        "funding_rate", "dollar_vol"}


def stats(r: np.ndarray, name: str) -> dict:
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return {}
    wins = r[r > 0].sum()
    losses = -r[r < 0].sum()
    return dict(name=name, n=len(r), mean_R=r.mean(), win=(r > 0).mean(),
                pf=wins / losses if losses else np.inf)


def main(top_frac: float = 0.05):
    df = pd.read_pickle("research/cache/perp4h_feat.pkl")
    df = df[df["dollar_vol"] > 3e5]
    feats = [c for c in df.columns if c not in SKIP and df[c].dtype.kind in "fi"]
    df = df.dropna(subset=["R_long", "R_short"])
    tr, te = df[df.ts < SPLIT], df[df.ts >= SPLIT]
    print(f"признаков {len(feats)}, TRAIN {len(tr):,}, TEST {len(te):,}")

    out = []
    models = {}
    for side in ("long", "short"):
        fit = tr.sample(min(400000, len(tr)), random_state=0)
        m = HistGradientBoostingRegressor(max_iter=150, learning_rate=0.06,
                                          max_depth=5, min_samples_leaf=1000,
                                          l2_regularization=1.0, random_state=0,
                                          early_stopping=False)
        m.fit(fit[feats], fit[f"R_{side}"])
        models[side] = m
        p_tr = m.predict(tr[feats])
        p_te = m.predict(te[feats])
        for tag, sub, p in (("TRAIN", tr, p_tr), ("TEST", te, p_te)):
            r = sub[f"R_{side}"].to_numpy()
            thr = np.quantile(p, 1 - top_frac)
            sel = p >= thr
            base = stats(r, f"{side} {tag} все")
            top = stats(r[sel], f"{side} {tag} топ-{int(top_frac*100)}%")
            out.append(base)
            out.append(top)
        ic = np.corrcoef(p_te, te[f"R_{side}"])[0, 1]
        print(f"{side}: корреляция прогноза с фактом на TEST = {ic:+.4f}")

    res = pd.DataFrame(out)
    print(res.to_string(index=False, float_format=lambda x: f"{x:,.4f}"))

    # какие признаки реально несут информацию (permutation importance на TEST)
    from sklearn.inspection import permutation_importance
    sample = te.sample(min(50000, len(te)), random_state=0)
    for side in ("long", "short"):
        pi = permutation_importance(models[side], sample[feats], sample[f"R_{side}"],
                                    n_repeats=1, random_state=0, n_jobs=4,
                                    scoring="neg_mean_squared_error")
        imp = pd.Series(pi.importances_mean, index=feats).sort_values(ascending=False)
        print(f"\nважность признаков ({side}, out-of-sample):")
        print(imp.head(15).to_string(float_format=lambda x: f"{x:.5f}"))


if __name__ == "__main__":
    main()
