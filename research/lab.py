"""
research/lab.py — быстрый исследовательский стенд.

Идея: один раз векторно считаем признаки и исходы «тройного барьера»,
потом за секунды проверяем сотни правил входа.

Данные — только реальные:
  * 1h спот Binance, 63 монеты, 2021-11 → 2026-05
    (backtest/data_cache/raw_uf/data/research_intradaybias_t1/ohlcv_cache)
  * 4h перп Binance, 290 монет, 2019 → 2026-09-01 + РЕАЛЬНЫЕ funding-ставки
    (backtest/data_cache/raw_uf/data/futures_universe)

Никаких синтетических рядов, никакого заглядывания в будущее:
признак на баре i считается по барам ≤ i, вход — по open бара i+1.
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPOT_DIR = os.path.join(ROOT, "backtest", "data_cache", "raw_uf", "data",
                        "research_intradaybias_t1", "ohlcv_cache")
PERP_DIR = os.path.join(ROOT, "backtest", "data_cache", "raw_uf", "data",
                        "futures_universe")
CACHE = os.path.join(ROOT, "research", "cache")
os.makedirs(CACHE, exist_ok=True)


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------

def load_perp_4h(min_bars: int = 3000) -> pd.DataFrame:
    """290 перп-монет, 4h, с funding. Возвращает длинную панель."""
    cache = os.path.join(CACHE, f"perp4h_{min_bars}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    ohlcv_dir = os.path.join(PERP_DIR, "ohlcv_4h")
    fund_dir = os.path.join(PERP_DIR, "funding_rates")
    frames = []
    for name in sorted(os.listdir(ohlcv_dir)):
        if not name.endswith("_4h.csv"):
            continue
        sym = name[: -len("_4h.csv")]
        df = pd.read_csv(os.path.join(ohlcv_dir, name))
        if len(df) < min_bars:
            continue
        df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df[["ts", "open", "high", "low", "close", "volume"]].dropna()
        df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
        # funding: 8-часовые точки → приводим к 4h сетке
        fpath = os.path.join(fund_dir, f"{sym}_funding.csv")
        if os.path.exists(fpath):
            fdf = pd.read_csv(fpath)
            fdf["ts"] = pd.to_datetime(fdf["funding_time"], unit="ms", utc=True)
            fdf = fdf[["ts", "funding_rate"]].dropna().sort_values("ts")
            df = pd.merge_asof(df.sort_values("ts"), fdf, on="ts",
                               direction="backward", tolerance=pd.Timedelta("12h"))
        else:
            df["funding_rate"] = np.nan
        df["coin"] = sym.replace("USDT", "")
        frames.append(df)
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["coin", "ts"]).reset_index(drop=True)
    panel.to_pickle(cache)
    return panel


def load_spot_1h(min_bars: int = 8000) -> pd.DataFrame:
    cache = os.path.join(CACHE, f"spot1h_{min_bars}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    frames = []
    for name in sorted(os.listdir(SPOT_DIR)):
        if not name.endswith("_USDT_1h.csv"):
            continue
        df = pd.read_csv(os.path.join(SPOT_DIR, name), parse_dates=["time"])
        if len(df) < min_bars:
            continue
        df = df.rename(columns={"time": "ts"}).dropna()
        df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
        df["coin"] = name[: -len("_USDT_1h.csv")]
        df["funding_rate"] = np.nan
        frames.append(df[["ts", "open", "high", "low", "close", "volume", "coin", "funding_rate"]])
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["coin", "ts"]).reset_index(drop=True)
    panel.to_pickle(cache)
    return panel


# ---------------------------------------------------------------------------
# Индикаторы (векторные, причинные)
# ---------------------------------------------------------------------------

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = _rma(d.clip(lower=0), n)
    dn = _rma((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return _rma(tr, n)


def _adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = _atr(df, n)
    pdi = 100 * _rma(pd.Series(plus, index=df.index), n) / atr
    mdi = 100 * _rma(pd.Series(minus, index=df.index), n) / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return _rma(dx, n)


def _slope(s: pd.Series, n: int) -> pd.Series:
    """Наклон линейной регрессии за n баров, нормированный на уровень."""
    x = np.arange(n)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()

    def f(w):
        return ((x - xm) * (w - w.mean())).sum() / denom
    return s.rolling(n).apply(f, raw=True) / s.abs()


def add_features(g: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = g["close"], g["high"], g["low"], g["volume"]
    atr = _atr(g, 14)
    g["atr"] = atr
    g["atrp"] = atr / c
    g["atr_pct_rank"] = g["atrp"].rolling(500, min_periods=100).rank(pct=True)

    for n in (20, 50, 200):
        g[f"ema{n}"] = _ema(c, n)
    g["d_ema20"] = (c - g["ema20"]) / atr
    g["d_ema50"] = (c - g["ema50"]) / atr
    g["d_ema200"] = (c - g["ema200"]) / atr
    g["ema_stack"] = (np.sign(g["ema20"] - g["ema50"]) + np.sign(g["ema50"] - g["ema200"])) / 2.0
    g["ema20_slope"] = (g["ema20"] - g["ema20"].shift(10)) / atr

    for n in (3, 6, 12, 24, 48, 72, 168):
        g[f"ret{n}"] = c.pct_change(n)
        g[f"ret{n}_atr"] = (c - c.shift(n)) / atr

    g["rsi"] = _rsi(c, 14)
    g["rsi_fast"] = _rsi(c, 7)
    g["rsi_slope"] = g["rsi"] - g["rsi"].shift(5)
    ema12, ema26 = _ema(c, 12), _ema(c, 26)
    macd = ema12 - ema26
    g["macd_hist"] = (macd - _ema(macd, 9)) / atr
    g["adx"] = _adx(g, 14)
    lo14, hi14 = l.rolling(14).min(), h.rolling(14).max()
    g["stoch"] = 100 * (c - lo14) / (hi14 - lo14).replace(0, np.nan)

    sma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    g["bb_pctb"] = (c - (sma20 - 2 * sd20)) / (4 * sd20).replace(0, np.nan)
    g["bb_width"] = (4 * sd20) / sma20
    g["bb_squeeze"] = g["bb_width"].rolling(200, min_periods=50).rank(pct=True)
    g["z20"] = (c - sma20) / sd20.replace(0, np.nan)

    # эффективность хода Кауфмана: тренд против пилы
    change = (c - c.shift(20)).abs()
    vol_path = c.diff().abs().rolling(20).sum()
    g["er"] = change / vol_path.replace(0, np.nan)

    dv = c * v
    g["dollar_vol"] = dv.rolling(24).mean()
    g["vol_ratio"] = v / v.rolling(20).mean().replace(0, np.nan)
    g["vol_z"] = (v - v.rolling(50).mean()) / v.rolling(50).std().replace(0, np.nan)
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    g["obv_slope"] = (obv - obv.shift(20)) / v.rolling(20).mean().replace(0, np.nan) / 20

    hi24, lo24 = h.rolling(24).max(), l.rolling(24).min()
    g["range_pos24"] = (c - lo24) / (hi24 - lo24).replace(0, np.nan)
    hi72, lo72 = h.rolling(72).max(), l.rolling(72).min()
    g["range_pos72"] = (c - lo72) / (hi72 - lo72).replace(0, np.nan)
    g["d_hi72_atr"] = (hi72 - c) / atr
    g["d_lo72_atr"] = (c - lo72) / atr

    # свеча
    body = (c - g["open"]).abs()
    rng = (h - l).replace(0, np.nan)
    g["body_frac"] = body / rng
    g["upper_wick"] = (h - np.maximum(c, g["open"])) / rng
    g["lower_wick"] = (np.minimum(c, g["open"]) - l) / rng

    g["hour"] = g["ts"].dt.hour
    g["dow"] = g["ts"].dt.dayofweek

    if "funding_rate" in g:
        fr = g["funding_rate"]
        g["fund"] = fr
        g["fund_z"] = (fr - fr.rolling(180, min_periods=30).mean()) / \
                      fr.rolling(180, min_periods=30).std().replace(0, np.nan)
        g["fund_cum"] = fr.rolling(18, min_periods=3).sum()
    return g


def build(panel: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for coin, g in panel.groupby("coin", sort=False):
        g = add_features(g.copy())
        g["coin"] = coin
        parts.append(g)
    out = pd.concat(parts).sort_index()
    # рыночный контекст: BTC и «ширина рынка»
    btc = out[out.coin == "BTC"].set_index("ts")
    if len(btc):
        m = pd.DataFrame(index=btc.index)
        m["btc_ret24"] = btc["ret24"]
        m["btc_ret72"] = btc["ret72"]
        m["btc_atrp"] = btc["atrp"]
        m["btc_above200"] = (btc["d_ema200"] > 0).astype(float)
        m["btc_adx"] = btc["adx"]
        out = out.merge(m, left_on="ts", right_index=True, how="left")
        out["rel_ret24"] = out["ret24"] - out["btc_ret24"]
    # кросс-секция: ранг импульса среди всех монет в этот час
    for col in ("ret24", "ret72", "vol_ratio", "atrp"):
        out[f"xrank_{col}"] = out.groupby("ts")[col].rank(pct=True)
    out["breadth"] = out.groupby("ts")["d_ema200"].transform(lambda s: (s > 0).mean())
    return out


# ---------------------------------------------------------------------------
# Тройной барьер: исход сделки в единицах R (с издержками)
# ---------------------------------------------------------------------------

def triple_barrier(g: pd.DataFrame, sl_atr: float, rr: float, horizon: int,
                   cost: float = 0.0016) -> tuple[np.ndarray, np.ndarray]:
    """
    Вход по open следующего бара. Стоп = sl_atr * ATR, цель = rr * стоп.
    Возвращает (R_long, R_short) — результат в R с учётом издержек.
    Если ни один барьер не задет за horizon баров — выход по close.
    Внутри бара при касании обоих барьеров считаем СТОП.
    """
    o = g["open"].to_numpy()
    hi = g["high"].to_numpy()
    lo = g["low"].to_numpy()
    cl = g["close"].to_numpy()
    atr = g["atr"].to_numpy()
    n = len(o)
    entry = np.roll(o, -1)
    entry[-1] = np.nan
    atrp = atr / np.where(entry > 0, entry, np.nan)
    risk = sl_atr * atr
    bad = ~np.isfinite(atrp) | (atrp < 2e-4)   # мёртвые/битые серии — не торгуем
    risk = np.where(bad, np.nan, risk)
    res = {}
    for side in (+1, -1):
        stop = entry - side * risk
        target = entry + side * rr * risk
        out = np.full(n, np.nan)
        done = np.zeros(n, dtype=bool)
        for k in range(1, horizon + 1):
            idx = np.arange(n) + 1 + k - 1  # бары со следующего после входа
            valid = idx < n
            ii = np.where(valid, idx, 0)
            hk, lk, ck = hi[ii], lo[ii], cl[ii]
            hit_stop = (lk <= stop) if side > 0 else (hk >= stop)
            hit_tp = (hk >= target) if side > 0 else (lk <= target)
            newly_stop = valid & ~done & hit_stop
            newly_tp = valid & ~done & hit_tp & ~hit_stop
            out[newly_stop] = -1.0
            out[newly_tp] = rr
            done |= newly_stop | newly_tp
            if k == horizon:
                idx_end = np.minimum(np.arange(n) + horizon, n - 1)
                rem = valid & ~done
                pnl = side * (cl[idx_end] - entry) / risk
                out[rem] = pnl[rem]
        # издержки в единицах R
        out = out - cost * entry / risk
        res[side] = out
    return res[+1], res[-1]


def add_labels(df: pd.DataFrame, sl_atr: float, rr: float, horizon: int,
               cost: float = 0.0016, suffix: str = "") -> pd.DataFrame:
    longs, shorts = [], []
    for _, g in df.groupby("coin", sort=False):
        rl, rs = triple_barrier(g, sl_atr, rr, horizon, cost)
        longs.append(pd.Series(rl, index=g.index))
        shorts.append(pd.Series(rs, index=g.index))
    df[f"R_long{suffix}"] = pd.concat(longs).sort_index()
    df[f"R_short{suffix}"] = pd.concat(shorts).sort_index()
    return df
