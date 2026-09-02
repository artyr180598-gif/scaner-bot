"""
research/portfolio.py — честный портфельный бэктест новой стратегии.

Отличие от статистики по барам: здесь моделируется РЕАЛЬНЫЙ счёт.
  * депозит, риск 1% на сделку, максимум N позиций одновременно;
  * одна позиция на монету, новые сигналы при занятом слоте пропускаются;
  * вход по open следующего бара (сигнал на закрытии), комиссия+проскальзывание;
  * стоп/цели проверяются по High/Low каждого бара, при спорном баре — СТОП;
  * частичные фиксации: 50% на TP1, 30% на TP2, 20% на TP3,
    после TP1 стоп переносится в безубыток;
  * выход по времени, если ничего не сработало.

Запуск:
    .venv/bin/python research/portfolio.py перп4h
    .venv/bin/python research/portfolio.py спот1h
"""
from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "/home/user/scaner-bot")

PANELS = {
    "перп4h": dict(path="research/cache/p4_compact.pkl", drop_win=6, horizon=24,
                   bars_per_day=6),
    "спот1h": dict(path="research/cache/s1_compact.pkl", drop_win=24, horizon=96,
                   bars_per_day=24),
}

COST = 0.0008          # комиссия+проскальзывание на одну ногу (0.08%)
RISK = 0.01            # риск 1% депозита на сделку
MAX_POS = 5            # максимум одновременных позиций
SL_ATR = 2.5           # стоп в ATR
TP_R = (1.0, 2.0, 3.5)  # цели в R
TP_FRAC = (0.5, 0.3, 0.2)


def conf_score(drop_w, atrp, dollar_vol, btc_ret24, vol_z):
    depth = np.clip((-drop_w - 0.15) / 0.20, 0, 1)
    vola = np.clip((atrp - 0.01) / 0.04, 0, 1)
    liq = np.clip((np.log10(np.clip(dollar_vol, 1e3, None)) - 5) / 2, 0, 1)
    mkt = np.clip((-btc_ret24 - 0.02) / 0.06, 0, 1)
    vol = np.clip((vol_z - 0.5) / 2.5, 0, 1)
    return 35 + 50 * (0.32 * depth + 0.22 * vola + 0.20 * liq + 0.16 * mkt + 0.10 * vol)


@dataclass
class Trade:
    coin: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    conf: float
    entry: float
    stop: float
    r_mult: float       # итог в R (взвешенный по частичным фиксациям)
    pnl_pct: float      # итог в % от депозита
    bars: int
    reason: str


def simulate(panel: str, min_conf: float = 55.0, verbose: bool = True,
             sl_atr: float = SL_ATR, tp_r=TP_R, tp_frac=TP_FRAC,
             breakeven: bool = True, max_pos: int = MAX_POS,
             horizon_mult: float = 1.0, drop_th: float = -0.15,
             d=None, signal_fn=None, side: str = "long"):
    cfg = PANELS[panel]
    if d is None:
        d = pd.read_pickle(cfg["path"]).sort_values(["coin", "ts"])
    d = d.copy()
    d["drop_w"] = d.groupby("coin", observed=True)["close"].transform(
        lambda s: s.pct_change(cfg["drop_win"]))
    if signal_fn is None:
        sig = ((d.drop_w < drop_th) & (d.vol_z > 0.5) & (d.btc_ret24 < -0.02)
               & (d.dollar_vol > 2e5) & (d.atrp > 0.005)).fillna(False)
        d["conf"] = conf_score(d.drop_w, d.atrp, d.dollar_vol, d.btc_ret24, d.vol_z)
    else:
        sig, conf = signal_fn(d)
        sig = sig.fillna(False)
        d["conf"] = conf
    d["signal"] = sig & (d.conf >= min_conf)

    # быстрый доступ к рядам по монете
    series = {}
    for coin, g in d.groupby("coin", observed=True, sort=False):
        series[coin] = dict(
            ts=g.ts.to_numpy(), o=g.open.to_numpy(float), h=g.high.to_numpy(float),
            l=g.low.to_numpy(float), c=g.close.to_numpy(float),
            atr=g.atr.to_numpy(float), sig=g.signal.to_numpy(bool),
            conf=g.conf.to_numpy(float))

    # хронологический список сигналов
    ev = d.loc[d.signal, ["ts", "coin", "conf"]].sort_values(["ts", "conf"],
                                                             ascending=[True, False])
    horizon = int(cfg["horizon"] * horizon_mult)
    open_until: dict[str, pd.Timestamp] = {}
    trades: list[Trade] = []
    equity = 1.0
    curve = []

    idx_cache = {c: {t: i for i, t in enumerate(s["ts"])} for c, s in series.items()}

    for ts, coin, conf in ev.itertuples(index=False):
        # слоты
        open_until_now = [t for t in open_until.values() if t > ts]
        if len(open_until_now) >= max_pos:
            continue
        if open_until.get(coin, pd.Timestamp.min.tz_localize("UTC")) > ts:
            continue
        s = series[coin]
        i = idx_cache[coin].get(ts)
        if i is None or i + 2 >= len(s["ts"]):
            continue
        sgn = 1.0 if side == "long" else -1.0
        entry = s["o"][i + 1] * (1 + sgn * COST)
        atr = s["atr"][i]
        if not np.isfinite(entry) or not np.isfinite(atr) or atr <= 0:
            continue
        risk = sl_atr * atr
        stop = entry - sgn * risk
        if stop <= 0:
            continue
        targets = [entry + sgn * r * risk for r in tp_r]
        n_t = len(tp_r)
        filled = [False] * n_t
        realized_R = 0.0
        remaining = 1.0
        cur_stop = stop
        reason = "время"
        end = min(i + 1 + horizon, len(s["ts"]) - 1)
        j = i + 1
        while j <= end:
            lo, hi = s["l"][j], s["h"][j]
            stop_hit = (lo <= cur_stop) if side == "long" else (hi >= cur_stop)
            if stop_hit:                            # стоп имеет приоритет
                r_at_stop = sgn * (cur_stop - entry) / risk
                realized_R += remaining * (r_at_stop - COST * entry / risk)
                remaining = 0.0
                reason = "стоп" if abs(cur_stop - entry) > 1e-12 and r_at_stop < 0 else "безубыток"
                break
            for k in range(n_t):
                tp_hit = (hi >= targets[k]) if side == "long" else (lo <= targets[k])
                if not filled[k] and tp_hit:
                    filled[k] = True
                    realized_R += tp_frac[k] * (tp_r[k] - COST * entry / risk)
                    remaining -= tp_frac[k]
                    if k == 0 and breakeven:
                        cur_stop = entry            # перенос в безубыток
            if remaining <= 1e-9:
                reason = "все цели"
                break
            j += 1
        if remaining > 1e-9 and reason == "время":
            exit_px = s["c"][min(j, end)]
            realized_R += remaining * (sgn * (exit_px - entry) / risk - COST * entry / risk)
        pnl = realized_R * RISK
        equity *= (1 + pnl)
        exit_ts = s["ts"][min(j, end)]
        trades.append(Trade(coin, ts, exit_ts, conf, entry, stop, realized_R,
                            100 * pnl, min(j, end) - i, reason))
        curve.append((exit_ts, equity))
        open_until[coin] = exit_ts

    t = pd.DataFrame([vars(x) for x in trades])
    if t.empty:
        print("сигналов нет")
        return t, pd.DataFrame()
    t["year"] = pd.to_datetime(t.entry_ts).dt.year
    if verbose:
        wins = t[t.r_mult > 0].r_mult.sum()
        loss = -t[t.r_mult < 0].r_mult.sum()
        days = (t.exit_ts.max() - t.entry_ts.min()).days
        print(f"\n=== ПОРТФЕЛЬНЫЙ ТЕСТ: {panel}, риск {RISK:.0%}/сделку, "
              f"до {max_pos} позиций, стоп {sl_atr}ATR, цели {tp_r} ===")
        print(f"сделок {len(t)} за {days} дней ({len(t)/max(1,days/30):.1f} в месяц)")
        print(f"win-rate {100*(t.r_mult>0).mean():.1f}%   средний R {t.r_mult.mean():+.3f}   "
              f"PF {wins/max(1e-9,loss):.2f}")
        print(f"итог по депозиту: ×{equity:.2f} ({100*(equity-1):+.1f}%), "
              f"средняя сделка {t.pnl_pct.mean():+.3f}% депозита")
        eq = pd.Series([c[1] for c in curve], index=[c[0] for c in curve]).sort_index()
        dd = (eq / eq.cummax() - 1).min()
        print(f"макс. просадка по закрытым сделкам: {100*dd:.1f}%")
        print(f"среднее удержание: {t.bars.mean():.1f} баров "
              f"({t.bars.mean()/cfg['bars_per_day']:.1f} суток)")
        print("\nисходы:", t.reason.value_counts().to_dict())
        print("\nпо годам:")
        g = t.groupby("year").agg(сделок=("r_mult", "size"), win=("r_mult", lambda x: (x > 0).mean()),
                                  R=("r_mult", "mean"), депозит=("pnl_pct", "sum"))
        g["PF"] = t.groupby("year").r_mult.apply(
            lambda x: x[x > 0].sum() / max(1e-9, -x[x < 0].sum()))
        print(g.round(3).to_string())
        print("\nпо уверенности:")
        b = pd.cut(t.conf, [0, 55, 60, 65, 70, 100])
        g2 = t.groupby(b, observed=True).agg(сделок=("r_mult", "size"),
                                             win=("r_mult", lambda x: (x > 0).mean()),
                                             R=("r_mult", "mean"))
        g2["PF"] = t.groupby(b, observed=True).r_mult.apply(
            lambda x: x[x > 0].sum() / max(1e-9, -x[x < 0].sum()))
        print(g2.round(3).to_string())
    return t, pd.DataFrame(curve, columns=["ts", "equity"])


if __name__ == "__main__":
    panel = sys.argv[1] if len(sys.argv) > 1 else "перп4h"
    t, c = simulate(panel)
    t.to_csv(f"research/cache/portfolio_{panel}.csv", index=False)
