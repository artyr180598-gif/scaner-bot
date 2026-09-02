"""
research/rules.py — турнир торговых сетапов.

Каждый сетап — понятное человеку правило («пробой с объёмом», «откат в
тренде», «сжатие Боллинджера»...). Для каждого считаем:
  n        — сколько раз срабатывал
  R        — средний результат в R (с издержками)
  alpha    — R минус средний R случайного входа той же стороны в тот же
             период (то есть очищенный от рыночного дрейфа)
  win, PF
  по 5 периодам — чтобы увидеть, держится ли эффект во времени.

Сетап считается кандидатом, только если alpha > 0 в БОЛЬШИНСТВЕ периодов,
включая последний (out-of-sample).
"""
from __future__ import annotations

import sys
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "/home/user/scaner-bot")

FOLDS = [("2019-2021", "2019-01-01", "2022-01-01"),
         ("2022", "2022-01-01", "2023-01-01"),
         ("2023", "2023-01-01", "2024-01-01"),
         ("2024", "2024-01-01", "2025-01-01"),
         ("2025", "2025-01-01", "2026-01-01"),
         ("2026", "2026-01-01", "2027-01-01")]


def setups(d: pd.DataFrame) -> dict[str, tuple[pd.Series, str]]:
    """Возвращает {название: (маска, сторона)}. Все условия — причинные."""
    s = {}
    up = d.ema_stack > 0
    dn = d.ema_stack < 0

    s["1. Пробой 72ч-максимума с объёмом"] = ((d.close >= d.high.rolling(72).max().shift(1)) & (d.vol_ratio > 1.5), "long")
    s["2. Пробой 72ч-минимума с объёмом"] = ((d.close <= d.low.rolling(72).min().shift(1)) & (d.vol_ratio > 1.5), "short")
    s["3. Откат к EMA20 в аптренде"] = (up & (d.d_ema20.abs() < 0.5) & (d.d_ema200 > 1) & (d.rsi > 40) & (d.rsi < 60), "long")
    s["4. Откат к EMA20 в даунтренде"] = (dn & (d.d_ema20.abs() < 0.5) & (d.d_ema200 < -1) & (d.rsi > 40) & (d.rsi < 60), "short")
    s["5. Выход из сжатия BB вверх"] = ((d.bb_squeeze < 0.2) & (d.bb_pctb > 0.9) & (d.vol_ratio > 1.3), "long")
    s["6. Выход из сжатия BB вниз"] = ((d.bb_squeeze < 0.2) & (d.bb_pctb < 0.1) & (d.vol_ratio > 1.3), "short")
    s["7. Перепроданность RSI<25"] = (d.rsi < 25, "long")
    s["8. Перекупленность RSI>75"] = (d.rsi > 75, "short")
    s["9. Перепроданность RSI<25 в аптренде"] = ((d.rsi < 25) & (d.d_ema200 > 0), "long")
    s["10. Перекупленность RSI>75 в даунтренде"] = ((d.rsi > 75) & (d.d_ema200 < 0), "short")
    s["11. Импульс 24ч топ-10% рынка"] = (d.xrank_ret24 > 0.9, "long")
    s["12. Импульс 24ч дно-10% рынка"] = (d.xrank_ret24 < 0.1, "short")
    s["13. Обратный: топ-10% импульса шорт"] = (d.xrank_ret24 > 0.9, "short")
    s["14. Обратный: дно-10% импульса лонг"] = (d.xrank_ret24 < 0.1, "long")
    s["15. Объёмный всплеск + рост"] = ((d.vol_z > 2) & (d.ret3 > 0), "long")
    s["16. Объёмный всплеск + падение"] = ((d.vol_z > 2) & (d.ret3 < 0), "short")
    s["17. Тихий рынок → шорт"] = ((d.vol_z < -0.8) & (d.adx < 20), "short")
    s["18. Тренд ADX>30 вверх"] = ((d.adx > 30) & up & (d.d_ema200 > 0), "long")
    s["19. Тренд ADX>30 вниз"] = ((d.adx > 30) & dn & (d.d_ema200 < 0), "short")
    s["20. Z-score < -2 (отскок)"] = (d.z20 < -2, "long")
    s["21. Z-score > +2 (откат)"] = (d.z20 > 2, "short")
    s["22. Funding перегрет → шорт"] = (d.fund_z > 1.5, "short")
    s["23. Funding отрицательный → лонг"] = (d.fund_z < -1.5, "long")
    s["24. Funding перегрет + рост цены → шорт"] = ((d.fund_z > 1) & (d.ret24 > 0.05), "short")
    s["25. Молот (длинная нижняя тень)"] = ((d.lower_wick > 0.6) & (d.ret24 < -0.03), "long")
    s["26. Падающая звезда"] = ((d.upper_wick > 0.6) & (d.ret24 > 0.03), "short")
    s["27. Слабость: ниже EMA200 + низкий объём"] = ((d.d_ema200 < -2) & (d.vol_z < -0.5), "short")
    s["28. Сила: выше EMA200 + объём"] = ((d.d_ema200 > 2) & (d.vol_z > 0.5), "long")
    s["29. Три растущих бара + объём"] = ((d.ret3 > 0) & (d.close > d.open) & (d.vol_ratio > 1.2) & up, "long")
    s["30. Три падающих бара + объём"] = ((d.ret3 < 0) & (d.close < d.open) & (d.vol_ratio > 1.2) & dn, "short")
    s["31. Разворот после капитуляции"] = ((d.ret24 < -0.15) & (d.vol_z > 1.5) & (d.lower_wick > 0.4), "long")
    s["32. Эйфория: +25% за сутки → шорт"] = ((d.ret24 > 0.25) & (d.vol_z > 1), "short")
    s["33. Высокая эффективность хода вверх"] = ((d.er > 0.4) & (d.ret24 > 0), "long")
    s["34. Высокая эффективность хода вниз"] = ((d.er > 0.4) & (d.ret24 < 0), "short")
    s["35. Медвежий рынок: шорт слабых"] = ((d.breadth < 0.3) & (d.xrank_ret24 < 0.3), "short")
    s["36. Бычий рынок: лонг сильных"] = ((d.breadth > 0.6) & (d.xrank_ret24 > 0.7), "long")
    s["37. Возврат в диапазон сверху"] = ((d.range_pos24 > 0.9) & (d.adx < 20), "short")
    s["38. Возврат в диапазон снизу"] = ((d.range_pos24 < 0.1) & (d.adx < 20), "long")
    s["39. BTC растёт → лонг сильной альты"] = ((d.btc_ret24 > 0.02) & (d.rel_ret24 > 0), "long")
    s["40. BTC падает → шорт слабой альты"] = ((d.btc_ret24 < -0.02) & (d.rel_ret24 < 0), "short")
    return s


def evaluate(d: pd.DataFrame, mask: pd.Series, side: str) -> dict:
    col = f"R_{side}"
    r = d.loc[mask, col].dropna()
    if len(r) < 200:
        return {}
    row = dict(n=len(r), R=r.mean(), win=(r > 0).mean(),
               pf=r[r > 0].sum() / max(1e-9, -r[r < 0].sum()))
    # alpha по каждому периоду и суммарно
    alphas = []
    for name, a, b in FOLDS:
        sl = (d.ts >= a) & (d.ts < b)
        base = d.loc[sl, col].mean()
        sel = d.loc[sl & mask, col]
        if len(sel) < 50 or not np.isfinite(base):
            row[name] = np.nan
            continue
        row[name] = sel.mean() - base
        alphas.append((len(sel), sel.mean() - base))
    if alphas:
        w = np.array([a[0] for a in alphas], float)
        row["alpha"] = float(np.average([a[1] for a in alphas], weights=w))
        row["pos_folds"] = int(sum(1 for a in alphas if a[1] > 0))
        row["folds"] = len(alphas)
    return row


def main(path="research/cache/p4_compact.pkl", tag="перп 4h"):
    d = pd.read_pickle(path)
    d = d.sort_values(["coin", "ts"])
    # rolling-величины, которых нет в признаках, считаем здесь по монетам
    d["hh72"] = d.groupby("coin", observed=True)["high"].transform(lambda s: s.rolling(72).max().shift(1))
    d["ll72"] = d.groupby("coin", observed=True)["low"].transform(lambda s: s.rolling(72).min().shift(1))
    rows = []
    S = setups(d)
    for name, (mask, side) in S.items():
        mask = mask.fillna(False)
        res = evaluate(d, mask, side)
        if res:
            res["setup"] = name
            res["side"] = side
            rows.append(res)
    r = pd.DataFrame(rows)
    cols = ["setup", "side", "n", "R", "alpha", "win", "pf", "pos_folds"] + [f[0] for f in FOLDS]
    r = r[cols].sort_values("alpha", ascending=False)
    pd.set_option("display.width", 250)
    print(f"=== ТУРНИР СЕТАПОВ ({tag}), SL 1.5ATR / TP 2R / 12 баров, издержки 0.16% ===")
    print("alpha = превышение над случайным входом той же стороны (очищено от дрейфа рынка)")
    print(r.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
    r.to_csv(f"research/cache/rules_{tag.replace(' ', '')}.csv", index=False)


if __name__ == "__main__":
    main(*(sys.argv[1:] or []))
