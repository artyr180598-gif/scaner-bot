"""
research/capitulation.py — детальное изучение единственного сетапа, который
подтвердился на ДВУХ независимых наборах данных (перп 4h и спот 1h):

    резкое падение + всплеск объёма + выкуп нижней тени → лонг

Здесь ищем: оптимальную геометрию сделки, условия, при которых сетап
работает лучше/хуже, и его зеркальную версию (перегрев → шорт).
"""
from __future__ import annotations

import sys
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "/home/user/scaner-bot")
from research.lab import triple_barrier   # noqa: E402

PANELS = {"перп 4h": ("research/cache/p4_compact.pkl", 6, 24),
          "спот 1h": ("research/cache/s1_compact.pkl", 24, 96)}


def folds(d):
    out = []
    for y in range(2019, 2027):
        m = (d.ts >= f"{y}-01-01") & (d.ts < f"{y+1}-01-01")
        if m.sum() > 5000:
            out.append((str(y)[2:], m))
    return out


def summarize(r: pd.Series, d: pd.DataFrame, mask: pd.Series, col: str, F) -> dict:
    o = dict(n=len(r), R=r.mean(), win=(r > 0).mean(),
             pf=r[r > 0].sum() / max(1e-9, -r[r < 0].sum()))
    pos = 0
    for nm, m in F:
        base = d.loc[m, col].mean()
        s = d.loc[m & mask, col]
        v = s.mean() - base if len(s) >= 40 else np.nan
        o[nm] = v
        pos += 1 if (v == v and v > 0) else 0
    o["pos"] = pos
    return o


def geometry_for(d, mask, side, panel, label):
    """Подбор стопа/цели/горизонта именно под этот сетап."""
    rows = []
    groups = [(g[["open", "high", "low", "close", "atr"]], g.index) for _, g in d.groupby("coin", observed=True, sort=False)]
    scale = 1 if panel == "перп 4h" else 4      # 1h баров в 4 раза больше
    for sl in (1.0, 1.5, 2.0, 3.0):
        for rr in (1.0, 1.5, 2.0, 3.0):
            for H in (3 * scale, 6 * scale, 12 * scale, 24 * scale, 48 * scale):
                parts = []
                for g, idx in groups:
                    rl, rs = triple_barrier(g, sl, rr, H)
                    parts.append(pd.Series(rl if side == "long" else rs, index=idx))
                r = pd.concat(parts).sort_index()
                sel = r[mask].dropna()
                if len(sel) < 200:
                    continue
                rows.append(dict(sl=sl, rr=rr, H=H, n=len(sel), R=sel.mean(),
                                 win=(sel > 0).mean(),
                                 pf=sel[sel > 0].sum() / max(1e-9, -sel[sel < 0].sum()),
                                 pct=100 * (sel * sl * d.loc[sel.index, "atrp"]).mean()))
    r = pd.DataFrame(rows).sort_values("pct", ascending=False)
    print(f"\n--- геометрия для «{label}» ({panel}), сортировка по среднему % на сделку")
    print(r.head(12).to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
    return r


def main():
    for panel, (path, w_short, w_long) in PANELS.items():
        d = pd.read_pickle(path).sort_values(["coin", "ts"])
        F = folds(d)
        d["drop_w"] = d.groupby("coin", observed=True)["close"].transform(lambda s: s.pct_change(w_short))
        d["rise_w"] = d["drop_w"]
        print(f"\n{'='*100}\nПАНЕЛЬ: {panel}   (окно импульса {w_short} баров)")

        base = ((d.drop_w < -0.18) & (d.vol_z > 0.5)).fillna(False)
        rows = []
        # 1) вклад каждого условия
        variants = {
            "только падение": (d.drop_w < -0.18),
            "+ объём": (d.drop_w < -0.18) & (d.vol_z > 0.5),
            "+ объём + нижняя тень": (d.drop_w < -0.18) & (d.vol_z > 0.5) & (d.lower_wick > 0.3),
            "+ объём + тень + бар зелёный": (d.drop_w < -0.18) & (d.vol_z > 0.5) & (d.lower_wick > 0.3) & (d.close > d.open),
            "+ объём + тень + RSI<30": (d.drop_w < -0.18) & (d.vol_z > 0.5) & (d.lower_wick > 0.3) & (d.rsi < 30),
            "+ объём + рынок тоже падал": (d.drop_w < -0.18) & (d.vol_z > 0.5) & (d.btc_ret24 < -0.02),
            "+ объём + рынок спокоен": (d.drop_w < -0.18) & (d.vol_z > 0.5) & (d.btc_ret24 > -0.02),
            "+ объём + вне сильного даунтренда": (d.drop_w < -0.18) & (d.vol_z > 0.5) & (d.d_ema200 > -8),
        }
        for name, m in variants.items():
            m = m.fillna(False)
            r = d.loc[m, "R_long"].dropna()
            if len(r) < 150:
                continue
            rows.append(dict(вариант=name, **summarize(r, d, m, "R_long", F)))
        print("\n--- из чего состоит сетап (лонг после обвала):")
        print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:+.3f}"))

        # 2) зеркало: перегрев → шорт
        rows = []
        mirrors = {
            "рост >18% + объём": (d.rise_w > 0.18) & (d.vol_z > 0.5),
            "рост >18% + объём + верхняя тень": (d.rise_w > 0.18) & (d.vol_z > 0.5) & (d.upper_wick > 0.3),
            "рост >25% + объём + верхняя тень": (d.rise_w > 0.25) & (d.vol_z > 0.5) & (d.upper_wick > 0.3),
            "рост >35% + объём": (d.rise_w > 0.35) & (d.vol_z > 0.5),
            "рост >25% + объём + красный бар": (d.rise_w > 0.25) & (d.vol_z > 0.5) & (d.close < d.open),
        }
        for name, m in mirrors.items():
            m = m.fillna(False)
            r = d.loc[m, "R_short"].dropna()
            if len(r) < 150:
                continue
            rows.append(dict(вариант=name, **summarize(r, d, m, "R_short", F)))
        print("\n--- зеркало (шорт после вертикального роста):")
        print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:+.3f}"))

        geometry_for(d, base, "long", panel, "обвал −18% + объём")
        del d


if __name__ == "__main__":
    main()
