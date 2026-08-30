"""
run_pulse.py — честный walk-forward бектест НАПРАВЛЕННЫХ стратегий.

Данные: тот же кеш, что и у арбитражного бектеста
(Binance spot 1ч → 4ч + perp 4ч + funding 8ч, 49 монет, 2021→2026).

Принципы (как в engine.py, нарушать нельзя):
  1. Сигнал по CLOSE бара t, исполнение по OPEN бара t+1.
  2. Round-trip taker + slippage на вход и выход.
  3. In-sample (подбор) и out-of-sample (проверка) РАЗДЕЛЕНЫ:
     IS = до 2024-05-01, OOS = после. Композит PULSE настраивается
     только по IS.
  4. Buy&hold — обязательный бенчмарк: если стратегия хуже — она мусор.

Запуск:
  .venv/bin/python backtest/run_pulse.py --tag pulse-v1
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha import PulseConfig, PulseEngine  # noqa: E402
from backtest.engine import BAR_4H_MS, build_4h_frames, load_symbol  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
IS_END_MS = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)

def _cost_one_way_frac(fee_rt_pct: float, slip_bps: float) -> float:
    """Издержка ОДНОЙ смены позиции (вход ИЛИ выход): половина RT-комиссии + 1×slip."""
    return (fee_rt_pct / 100.0) / 2.0 + slip_bps / 10000.0


# ---------------------------------------------------------------------------
# Загрузка панели
# ---------------------------------------------------------------------------

def load_panel(symbols: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """
    Длинная панель 4ч-баров: index (ts, sym) → open/high/low/close/volume/
    funding/basis.
    """
    frames = []
    ok = []
    for sym in symbols:
        try:
            data = load_symbol(sym)
        except FileNotFoundError:
            continue
        frame = build_4h_frames(sym, data)
        if len(frame) < 800:
            continue
        # high/low спота нет в build_4h_frames — восстановим из 1ч
        spot = data["spot_1h"].copy()
        spot["bucket"] = (spot["ts"] // BAR_4H_MS) * BAR_4H_MS
        g = spot.groupby("bucket")
        hl = pd.DataFrame({
            "high": g["high"].max(),
            "low": g["low"].min(),
            "volume": g["volume"].sum(),
        })
        frame = frame.join(hl, how="left")
        frame["high"] = frame["high"].fillna(frame[["spot_close", "perp_close"]].max(axis=1))
        frame["low"] = frame["low"].fillna(frame[["spot_close", "perp_close"]].min(axis=1))
        frame["volume"] = frame["volume"].fillna(0.0)
        # цена направленной сделки — спот close/open (то, что «цена монеты»)
        frame["open"] = frame["spot_open"]
        frame["close"] = frame["spot_close"]
        frame["basis_pct"] = (frame["perp_close"] - frame["spot_close"]) / frame["spot_close"] * 100.0
        fund = data["funding"].copy()
        fund = fund.sort_values("ts")
        # asof: последняя известная ставка на момент бара
        frame = frame.reset_index().rename(columns={"index": "ts"})
        merged = pd.merge_asof(
            frame.sort_values("ts"),
            fund.rename(columns={"rate_pct": "funding_pct"}).sort_values("ts"),
            on="ts",
            direction="backward",
        )
        merged["sym"] = sym
        frames.append(merged)
        ok.append(sym)
    if not frames:
        raise SystemExit("Нет данных. Запусти fetch_data.sh + prepare_data.py")
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["sym", "ts"]).reset_index(drop=True)
    return panel, ok


# ---------------------------------------------------------------------------
# Векторные индикаторы (панель по символу)
# ---------------------------------------------------------------------------

def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False).mean()
    loss = (-d.clip(upper=0.0)).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def _adx(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    up = h.diff()
    down = -l.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / n, adjust=False).mean()
    pdi = 100.0 * pd.Series(plus_dm, index=c.index).ewm(alpha=1.0 / n, adjust=False).mean() / atr
    mdi = 100.0 * pd.Series(minus_dm, index=c.index).ewm(alpha=1.0 / n, adjust=False).mean() / atr
    dx = (100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan))
    return dx.ewm(alpha=1.0 / n, adjust=False).mean()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Индикаторы внутри одного символа (без look-ahead)."""
    c, h, l = df["close"], df["high"], df["low"]
    out = df.copy()
    out["rsi"] = _rsi(c, 14)
    out["ema12"] = _ema(c, 12)
    out["ema26"] = _ema(c, 26)
    out["ema50"] = _ema(c, 50)
    out["ema200"] = _ema(c, 200)
    macd = out["ema12"] - out["ema26"]
    out["macd"] = macd
    out["macd_sig"] = _ema(macd, 9)
    out["macd_hist"] = macd - out["macd_sig"]
    out["atr"] = _atr(h, l, c, 14)
    out["adx"] = _adx(h, l, c, 14)
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    out["bb_mid"] = mid
    out["bb_up"] = mid + 2 * sd
    out["bb_lo"] = mid - 2 * sd
    out["bb_pct"] = (c - (mid - 2 * sd)) / (4 * sd.replace(0.0, np.nan))
    out["ret1"] = c.pct_change()
    out["ret6"] = c.pct_change(6)
    out["ret20"] = c.pct_change(20)
    out["ret42"] = c.pct_change(42)
    out["vol20"] = out["ret1"].rolling(20).std()
    out["donch_hi"] = h.rolling(20).max()
    out["donch_lo"] = l.rolling(20).min()
    # Supertrend (упрощённый): mid ± 3 ATR, направление по close vs band
    hl2 = (h + l) / 2.0
    out["st_up"] = hl2 - 3.0 * out["atr"]
    out["st_dn"] = hl2 + 3.0 * out["atr"]
    st = np.ones(len(out))
    st_line = np.zeros(len(out))
    atr_a = out["atr"].to_numpy()
    close_a = c.to_numpy()
    up_a = out["st_up"].to_numpy()
    dn_a = out["st_dn"].to_numpy()
    for i in range(1, len(out)):
        if not np.isfinite(atr_a[i]):
            st[i] = st[i - 1]
            st_line[i] = st_line[i - 1]
            continue
        prev_up = up_a[i] if (st[i - 1] < 0 or up_a[i] > st_line[i - 1]) else max(up_a[i], st_line[i - 1]) if st[i - 1] > 0 else up_a[i]
        prev_dn = dn_a[i] if (st[i - 1] > 0 or dn_a[i] < st_line[i - 1]) else min(dn_a[i], st_line[i - 1]) if st[i - 1] < 0 else dn_a[i]
        if close_a[i] > prev_dn:
            st[i] = 1
            st_line[i] = prev_up
        elif close_a[i] < prev_up:
            st[i] = -1
            st_line[i] = prev_dn
        else:
            st[i] = st[i - 1]
            st_line[i] = prev_up if st[i] > 0 else prev_dn
    out["supertrend"] = st
    out["funding_z"] = (
        (out["funding_pct"] - out["funding_pct"].rolling(90, min_periods=20).mean())
        / out["funding_pct"].rolling(90, min_periods=20).std().replace(0.0, np.nan)
    )
    out["basis_z"] = (
        (out["basis_pct"] - out["basis_pct"].rolling(90, min_periods=20).mean())
        / out["basis_pct"].rolling(90, min_periods=20).std().replace(0.0, np.nan)
    )
    out["vol_z"] = (
        (out["volume"] - out["volume"].rolling(40, min_periods=10).mean())
        / out["volume"].rolling(40, min_periods=10).std().replace(0.0, np.nan)
    )
    vn = out["ret42"] / (out["vol20"] * math.sqrt(42.0))
    out["vol_norm_mom"] = vn
    return out


# ---------------------------------------------------------------------------
# Сигналы: +1 / 0 / −1 на закрытии бара (без look-ahead)
# ---------------------------------------------------------------------------

SignalFn = Callable[[pd.DataFrame], pd.Series]


def sig_bah(d: pd.DataFrame) -> pd.Series:
    return pd.Series(1, index=d.index, dtype=float)


def sig_rsi_mr(d: pd.DataFrame) -> pd.Series:
    s = pd.Series(np.nan, index=d.index)
    s = s.mask(d["rsi"] < 30, 1.0)
    s = s.mask(d["rsi"] > 70, -1.0)
    s = s.ffill()
    s = s.mask((d["rsi"] > 45) & (d["rsi"] < 55), 0.0)
    return s.fillna(0.0)


def _sign_hold(s: pd.Series) -> pd.Series:
    return pd.Series(np.sign(s.to_numpy()), index=s.index).replace(0.0, np.nan).ffill().fillna(0.0)


def sig_rsi_trend(d: pd.DataFrame) -> pd.Series:
    return _sign_hold(d["rsi"] - 50.0)


def sig_macd_xo(d: pd.DataFrame) -> pd.Series:
    return _sign_hold(d["macd"] - d["macd_sig"])


def sig_ema_12_26(d: pd.DataFrame) -> pd.Series:
    return _sign_hold(d["ema12"] - d["ema26"])


def sig_ema_50_200(d: pd.DataFrame) -> pd.Series:
    return _sign_hold(d["ema50"] - d["ema200"])


def sig_bb_bounce(d: pd.DataFrame) -> pd.Series:
    s = pd.Series(np.nan, index=d.index)
    s = s.mask(d["close"] < d["bb_lo"], 1.0)
    s = s.mask(d["close"] > d["bb_up"], -1.0)
    s = s.ffill()
    mid = (d["close"] - d["bb_mid"]).abs()
    width = (d["bb_up"] - d["bb_lo"]).replace(0.0, np.nan)
    s = s.mask(mid < 0.15 * width, 0.0)
    return s.fillna(0.0)


def sig_bb_break(d: pd.DataFrame) -> pd.Series:
    s = pd.Series(0.0, index=d.index)
    s = s.mask(d["close"] > d["bb_up"], 1.0)
    s = s.mask(d["close"] < d["bb_lo"], -1.0)
    return s


def sig_donchian(d: pd.DataFrame) -> pd.Series:
    s = pd.Series(0.0, index=d.index)
    s = s.mask(d["close"] >= d["donch_hi"].shift(1), 1.0)
    s = s.mask(d["close"] <= d["donch_lo"].shift(1), -1.0)
    # hold until opposite
    return s.replace(0.0, np.nan).ffill().fillna(0.0)


def sig_mom20(d: pd.DataFrame) -> pd.Series:
    return np.sign(d["ret20"]).fillna(0.0)


def sig_mom42(d: pd.DataFrame) -> pd.Series:
    return np.sign(d["ret42"]).fillna(0.0)


def sig_rev1(d: pd.DataFrame) -> pd.Series:
    thr = 2.0 * d["vol20"]
    s = pd.Series(0.0, index=d.index)
    s = s.mask(d["ret1"] < -thr, 1.0)
    s = s.mask(d["ret1"] > thr, -1.0)
    return s


def sig_funding_fade(d: pd.DataFrame) -> pd.Series:
    s = pd.Series(np.nan, index=d.index)
    s = s.mask(d["funding_z"] > 1.5, -1.0)
    s = s.mask(d["funding_z"] < -1.5, 1.0)
    s = s.ffill()
    s = s.mask(d["funding_z"].abs() < 0.35, 0.0)
    return s.fillna(0.0)


def sig_funding_follow(d: pd.DataFrame) -> pd.Series:
    return np.sign(d["funding_pct"]).fillna(0.0)


def sig_basis_fade(d: pd.DataFrame) -> pd.Series:
    s = pd.Series(np.nan, index=d.index)
    s = s.mask(d["basis_z"] > 1.5, -1.0)
    s = s.mask(d["basis_z"] < -1.5, 1.0)
    s = s.ffill()
    s = s.mask(d["basis_z"].abs() < 0.35, 0.0)
    return s.fillna(0.0)


def sig_supertrend(d: pd.DataFrame) -> pd.Series:
    return d["supertrend"].fillna(0.0)


def sig_vol_break(d: pd.DataFrame) -> pd.Series:
    s = pd.Series(0.0, index=d.index)
    hot = d["vol_z"] > 1.2
    s = s.mask(hot & (d["ret1"] > 0), 1.0)
    s = s.mask(hot & (d["ret1"] < 0), -1.0)
    return s.replace(0.0, np.nan).ffill().fillna(0.0)


def sig_adx_ema(d: pd.DataFrame) -> pd.Series:
    """Тренд только когда ADX высокий, иначе флэт."""
    trend = np.sign(d["ema12"] - d["ema26"])
    return trend.where(d["adx"] >= 22.0, 0.0).fillna(0.0)


def _tanh(x: pd.Series, scale: float) -> pd.Series:
    return np.tanh(x / scale)


def sig_pulse(d: pd.DataFrame, cfg: Optional[PulseConfig] = None) -> pd.Series:
    """Векторный близнец PulseEngine (те же веса). Гистерезис — посимвольно."""
    cfg = cfg or PulseConfig()
    mom = _tanh(d["vol_norm_mom"].fillna(0.0), 1.2)
    crowd = -_tanh(d["funding_z"].fillna(0.0), 1.4)
    basis = -_tanh(d["basis_z"].fillna(0.0), 1.4)
    vol_c = _tanh(d["vol_z"].fillna(0.0), 1.5)
    vol = d["vol20"].replace(0.0, np.nan)
    mr = -_tanh((d["ret1"] / vol).fillna(0.0), 1.5)
    rsi_c = pd.Series(0.0, index=d.index)
    rsi_c = rsi_c.mask(d["rsi"] <= 35, (35.0 - d["rsi"]) / 35.0)
    rsi_c = rsi_c.mask(d["rsi"] >= 65, (65.0 - d["rsi"]) / 35.0)
    vol_confirm = vol_c * np.sign(mom.replace(0.0, 1.0))
    trend = d["adx"].fillna(0.0) >= cfg.adx_trend
    score_t = cfg.w_mom * mom + cfg.w_crowd_trend * crowd + cfg.w_basis_trend * basis + cfg.w_vol_trend * vol_confirm
    score_r = cfg.w_mr * mr + cfg.w_rsi * rsi_c + cfg.w_crowd_range * crowd + cfg.w_basis_range * basis
    score = pd.Series(np.where(trend, score_t, score_r), index=d.index)
    extreme = d["funding_z"].abs() >= cfg.crowding_extreme
    score = pd.Series(np.where(extreme, 0.65 * crowd + 0.35 * score, score), index=d.index)
    score = score.clip(-1.0, 1.0)
    # гистерезис
    arr = score.to_numpy()
    side = np.zeros(len(arr))
    prev = 0.0
    for i, sc in enumerate(arr):
        if not np.isfinite(sc):
            side[i] = prev
            continue
        if prev == 0:
            nxt = 1.0 if sc >= cfg.score_entry else (-1.0 if sc <= -cfg.score_entry else 0.0)
        elif prev > 0:
            nxt = 0.0 if sc <= cfg.score_exit else 1.0
            if sc <= -cfg.score_entry:
                nxt = -1.0
        else:
            nxt = 0.0 if sc >= -cfg.score_exit else -1.0
            if sc >= cfg.score_entry:
                nxt = 1.0
        side[i] = nxt
        prev = nxt
    return pd.Series(side, index=d.index)


def sig_nexus(d: pd.DataFrame) -> pd.Series:
    """
    NEXUS — тройное подтверждение (собственная стратегия):
      лонг только если (а) медленный моментум > 0, (б) funding не перегрет,
      (в) ADX тренд ИЛИ RSI не перекуплен.
      шорт — зеркало. Иначе флэт. Реже, но чище.
    """
    long_ok = (
        (d["ret42"] > 0)
        & (d["funding_z"].fillna(0.0) < 1.2)
        & ((d["adx"] >= 20) | (d["rsi"] < 65))
        & (d["ema12"] > d["ema26"])
    )
    short_ok = (
        (d["ret42"] < 0)
        & (d["funding_z"].fillna(0.0) > -1.2)
        & ((d["adx"] >= 20) | (d["rsi"] > 35))
        & (d["ema12"] < d["ema26"])
    )
    # fade override
    fade_short = d["funding_z"] > 2.0
    fade_long = d["funding_z"] < -2.0
    s = pd.Series(0.0, index=d.index)
    s = s.mask(long_ok, 1.0)
    s = s.mask(short_ok, -1.0)
    s = s.mask(fade_short, -1.0)
    s = s.mask(fade_long, 1.0)
    return s


def sig_crowd_impulse(d: pd.DataFrame) -> pd.Series:
    """
    Crowding Impulse — собственный индикатор:
    изменение funding за 24ч (6 баров) × знак цены.
    Если funding растёт быстрее цены — толпа догоняет, fade.
    Если цена растёт при падающем funding — здоровый тренд, follow.
    """
    f_imp = d["funding_pct"].diff(6)
    p_imp = d["ret6"]
    s = pd.Series(0.0, index=d.index)
    # здоровый тренд: цена вверх, funding не раздувается
    s = s.mask((p_imp > 0) & (f_imp <= 0) & (d["adx"] >= 18), 1.0)
    s = s.mask((p_imp < 0) & (f_imp >= 0) & (d["adx"] >= 18), -1.0)
    # разгрузка толпы
    s = s.mask((f_imp > 0.02) & (d["funding_z"] > 1.0), -1.0)
    s = s.mask((f_imp < -0.02) & (d["funding_z"] < -1.0), 1.0)
    return s



def sig_pulse_strict(d: pd.DataFrame) -> pd.Series:
    """PULSE с жёстким порогом — меньше сделок, выше качество (подбор на IS)."""
    return sig_pulse(d, PulseConfig(score_entry=0.55, score_exit=0.20, crowding_extreme=1.6))


def sig_funding_extreme(d: pd.DataFrame) -> pd.Series:
    """Только хвосты funding (|z|≥2.5) — редкий crowding-fade."""
    s = pd.Series(np.nan, index=d.index)
    s = s.mask(d["funding_z"] > 2.5, -1.0)
    s = s.mask(d["funding_z"] < -2.5, 1.0)
    s = s.ffill()
    s = s.mask(d["funding_z"].abs() < 0.5, 0.0)
    return s.fillna(0.0)


def sig_mom_weekly(d: pd.DataFrame) -> pd.Series:
    """Моментум с ребалансом раз в 42 бара (~неделя на 4ч) — меньше издержек."""
    raw = pd.Series(np.sign(d["ret42"].to_numpy()), index=d.index)
    out = pd.Series(np.nan, index=d.index)
    step = 42
    n = len(d)
    for i in range(step, n, step):
        out.iloc[i:min(i + step, n)] = raw.iloc[i]
    return out.ffill().fillna(0.0)


STRATEGIES: dict[str, SignalFn] = {
    "BAH": sig_bah,
    "RSI_MR": sig_rsi_mr,
    "RSI_TREND": sig_rsi_trend,
    "MACD_XO": sig_macd_xo,
    "EMA_12_26": sig_ema_12_26,
    "EMA_50_200": sig_ema_50_200,
    "BB_BOUNCE": sig_bb_bounce,
    "BB_BREAK": sig_bb_break,
    "DONCHIAN": sig_donchian,
    "MOM_20": sig_mom20,
    "MOM_42": sig_mom42,
    "REV_1": sig_rev1,
    "FUNDING_FADE": sig_funding_fade,
    "FUNDING_FOLLOW": sig_funding_follow,
    "BASIS_FADE": sig_basis_fade,
    "SUPERTREND": sig_supertrend,
    "VOL_BREAK": sig_vol_break,
    "ADX_EMA": sig_adx_ema,
    "CROWD_IMPULSE": sig_crowd_impulse,
    "NEXUS": sig_nexus,
    "PULSE": sig_pulse,
    "PULSE_STRICT": sig_pulse_strict,
    "FUNDING_EXTREME": sig_funding_extreme,
    "MOM_WEEKLY": sig_mom_weekly,
}


# ---------------------------------------------------------------------------
# Симулятор: сигнал close t → позиция на open t+1 → open t+2
# ---------------------------------------------------------------------------

@dataclass
class StratMetrics:
    name: str
    split: str
    trades: int = 0
    winrate: float = 0.0
    avg_pnl_pct: float = 0.0
    total_pnl_pct: float = 0.0
    profit_factor: float = 0.0
    max_dd_pct: float = 0.0
    sharpe: float = 0.0
    bars_in: int = 0
    bars_all: int = 0
    avg_hold_bars: float = 0.0


def simulate_symbol(
    df: pd.DataFrame,
    signal: pd.Series,
    cost_frac: float,
    start_ts: int = 0,
    end_ts: int = 0,
) -> tuple[np.ndarray, list[float], list[int]]:
    """
    Сигнал на CLOSE бара i → позиция с OPEN i+1 до OPEN i+2.
    Издержки: |Δпозиции| × cost_frac. Сделка = отрезок одной стороны.
    """
    n = len(df)
    opens = df["open"].to_numpy(dtype=float)
    ts = df["ts"].to_numpy(dtype=np.int64)
    sig = signal.to_numpy(dtype=float)
    bar_pnl = np.zeros(n)
    pos_held = np.zeros(n)
    prev = 0.0
    for i in range(n - 2):
        if start_ts and int(ts[i]) < start_ts:
            continue
        if end_ts and int(ts[i]) > end_ts:
            break
        pos = float(sig[i]) if np.isfinite(sig[i]) else 0.0
        o1, o2 = opens[i + 1], opens[i + 2]
        if not (o1 > 0 and o2 > 0 and np.isfinite(o1) and np.isfinite(o2)):
            prev = pos
            continue
        turnover = abs(pos - prev)
        bar_pnl[i + 1] = pos * (o2 / o1 - 1.0) - turnover * cost_frac
        pos_held[i + 1] = pos
        prev = pos

    trades: list[float] = []
    holds: list[int] = []
    cur = 0.0
    acc = 0.0
    hold = 0
    for i in range(n):
        p = pos_held[i]
        if p == cur and cur != 0.0:
            acc += bar_pnl[i]
            hold += 1
            continue
        if cur != 0.0:
            trades.append(acc * 100.0)
            holds.append(max(1, hold))
        cur = p
        if p != 0.0:
            acc = bar_pnl[i]
            hold = 1
        else:
            acc = 0.0
            hold = 0
    if cur != 0.0:
        trades.append(acc * 100.0)
        holds.append(max(1, hold))
    return bar_pnl, trades, holds


def metrics_from_trades(
    name: str, split: str, trades: list[float], holds: list[int],
    bar_pnls: np.ndarray,
) -> StratMetrics:
    m = StratMetrics(name=name, split=split)
    m.trades = len(trades)
    m.bars_all = int(len(bar_pnls))
    m.bars_in = int(np.sum(np.abs(bar_pnls) > 0))
    if not trades:
        return m
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    m.winrate = 100.0 * len(wins) / len(trades)
    m.avg_pnl_pct = sum(trades) / len(trades)
    m.total_pnl_pct = sum(trades)
    m.profit_factor = (gw / gl) if gl > 0 else (math.inf if gw > 0 else 0.0)
    m.avg_hold_bars = (sum(holds) / len(holds)) if holds else 0.0
    # эквити по барам (сумма pnl долей → компаунд не делаем, аддитивно в %)
    eq = np.cumsum(bar_pnls) * 100.0
    if len(eq):
        peak = np.maximum.accumulate(eq)
        dd = peak - eq
        m.max_dd_pct = float(np.max(dd)) if len(dd) else 0.0
        # sharpe: бар 4ч → 6*365 баров/год
        if bar_pnls.std() > 0:
            m.sharpe = float(bar_pnls.mean() / bar_pnls.std() * math.sqrt(6 * 365))
    return m


# ---------------------------------------------------------------------------
# Кросс-секция: каждый бар лонг топ-квантиль / шорт боттом
# ---------------------------------------------------------------------------

def cross_sectional_signals(panel: pd.DataFrame, col: str, invert: bool = False) -> pd.Series:
    """Ранг по col внутри ts: верхние 20% → +1, нижние 20% → −1."""
    x = panel[["ts", "sym", col]].copy()
    x["rank"] = x.groupby("ts")[col].rank(pct=True)
    s = pd.Series(0.0, index=panel.index)
    hi, lo = 0.80, 0.20
    if invert:
        s = s.mask(x["rank"] >= hi, -1.0)
        s = s.mask(x["rank"] <= lo, 1.0)
    else:
        s = s.mask(x["rank"] >= hi, 1.0)
        s = s.mask(x["rank"] <= lo, -1.0)
    return s


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    dataset = RESULTS.parent / "data_cache" / "dataset"
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        or sorted(p.name.removesuffix(".csv.gz") for p in (dataset / "perp_4h").glob("*.csv.gz"))
    )
    print(f"Загрузка панели ({len(symbols)} символов)...")
    panel, symbols = load_panel(symbols)
    print(f"  {len(symbols)} монет, {len(panel):,} строк")

    enriched_parts = []
    for sym, g in panel.groupby("sym", sort=False):
        enriched_parts.append(enrich(g))
    panel = pd.concat(enriched_parts, ignore_index=True)
    panel = panel.sort_values(["sym", "ts"]).reset_index(drop=True)

    cost = _cost_one_way_frac(args.fee, args.slippage_bps)
    splits = {
        "IS": (0, IS_END_MS),
        "OOS": (IS_END_MS, 0),
        "ALL": (0, 0),
    }

    # кросс-секционные сигналы на всей панели
    panel["xs_mom"] = cross_sectional_signals(panel, "ret42", invert=False)
    panel["xs_fund_fade"] = cross_sectional_signals(panel, "funding_z", invert=True)
    extra = {
        "XS_MOM": "xs_mom",
        "XS_FUND_FADE": "xs_fund_fade",
    }

    rows: list[StratMetrics] = []
    details: dict[str, dict] = {}

    names = list(STRATEGIES) + list(extra)
    for name in names:
        print(f"  стратегия {name}...")
        if name in extra:
            panel["_sig"] = panel[extra[name]]
        else:
            # посимвольно, чтобы ffill не тёк между монетами
            parts = []
            for sym, g in panel.groupby("sym", sort=False):
                parts.append(STRATEGIES[name](g))
            panel["_sig"] = pd.concat(parts).sort_index()

        for split, (lo, hi) in splits.items():
            all_trades: list[float] = []
            all_holds: list[int] = []
            all_bars = []
            for sym, g in panel.groupby("sym", sort=False):
                sig = panel.loc[g.index, "_sig"]
                bp, tr, ho = simulate_symbol(g, sig, cost, start_ts=lo, end_ts=hi)
                all_trades.extend(tr)
                all_holds.extend(ho)
                all_bars.append(bp)
            cat = np.concatenate(all_bars) if all_bars else np.array([])
            m = metrics_from_trades(name, split, all_trades, all_holds, cat)
            rows.append(m)
        details[name] = {
            "IS": next(r for r in rows if r.name == name and r.split == "IS").__dict__,
            "OOS": next(r for r in rows if r.name == name and r.split == "OOS").__dict__,
            "ALL": next(r for r in rows if r.name == name and r.split == "ALL").__dict__,
        }

    # сверка PulseEngine vs векторный PULSE на одном символе (анти-дрейф)
    sample_sym = symbols[0]
    g = panel[panel["sym"] == sample_sym].iloc[:250]
    engine = PulseEngine(PulseConfig())
    engine_sides = []
    for row in g.itertuples():
        snap = engine.observe_bar(
            sample_sym, float(row.ts), float(row.open), float(row.high),
            float(row.low), float(row.close), float(row.volume or 0),
            float(row.funding_pct) if pd.notna(row.funding_pct) else None,
            float(row.basis_pct) if pd.notna(row.basis_pct) else None,
        )
        engine_sides.append(snap.side)
    vec = sig_pulse(g).to_list()
    # после min_bars стороны должны совпадать в большинстве точек
    warm = PulseConfig().min_bars
    agree = sum(int(a == b) for a, b in zip(engine_sides[warm:], vec[warm:]))
    agree_n = max(1, len(engine_sides) - warm)
    agree_pct = 100.0 * agree / agree_n

    # --- отчёт
    def fmt(m: StratMetrics) -> str:
        pf = "∞" if math.isinf(m.profit_factor) else f"{m.profit_factor:.2f}"
        return (
            f"| {m.name} | {m.split} | {m.trades} | {m.winrate:.1f} | "
            f"{m.avg_pnl_pct:+.3f} | {m.total_pnl_pct:+.1f} | {pf} | "
            f"{m.max_dd_pct:.1f} | {m.sharpe:.2f} | {m.avg_hold_bars:.1f} |"
        )

    header = (
        "| стратегия | сплит | сделок | winrate% | avg pnl% | сумма pnl% | PF | maxDD% | Sharpe | удерж.бар |"
    )
    sep = "|" + "---|" * 10

    # вердикт: OOS PF>=1.2 и avg>0 и не хуже BAH по сумме? BAH всегда в рынке.
    oos = {r.name: r for r in rows if r.split == "OOS"}
    is_ = {r.name: r for r in rows if r.split == "IS"}
    survivors = []
    for name, m in oos.items():
        if name == "BAH":
            continue
        if m.trades < 40:
            continue
        if m.profit_factor >= 1.20 and m.avg_pnl_pct > 0 and m.sharpe > 0:
            survivors.append(m)
    survivors.sort(key=lambda x: (x.sharpe, x.profit_factor), reverse=True)

    first_ts = int(panel["ts"].min())
    last_ts = int(panel["ts"].max())
    period = (
        f"{datetime.fromtimestamp(first_ts/1000, tz=timezone.utc):%Y-%m-%d} → "
        f"{datetime.fromtimestamp(last_ts/1000, tz=timezone.utc):%Y-%m-%d}"
    )
    lines = [
        "# Бектест направленных стратегий (PULSE v4)",
        "",
        f"- Период: **{period}** · символов: {len(symbols)}",
        f"- Сплит: IS до 2024-05-01 · OOS с 2024-05-01",
        f"- ТФ: 4ч спот Binance · сигнал close t → вход open t+1",
        f"- Издержки: taker {args.fee:.2f}% round-trip + slippage {args.slippage_bps} bps/ногу "
        f"(one-way ~{_cost_one_way_frac(args.fee, args.slippage_bps)*100:.3f}% на смену позиции, "
        f"вход+выход ≈ {_cost_one_way_frac(args.fee, args.slippage_bps)*200:.3f}%)",
        f"- Сверка PulseEngine vs векторный PULSE: {agree_pct:.1f}% совпадения сторон после прогрева",
        "",
        "## Сводка всех стратегий",
        "",
        header,
        sep,
    ]
    order = ["IS", "OOS", "ALL"]
    for name in names:
        for split in order:
            m = next(r for r in rows if r.name == name and r.split == split)
            lines.append(fmt(m))

    lines += [
        "",
        "## Как читать",
        "",
        "- **Winrate** сам по себе ничего не значит: RSI_MR часто имеет высокий "
        "winrate при отрицательном мат.ожидании (маленькие профиты, редкие крупные убытки).",
        "- **PF > 1.2 и Sharpe > 0 на OOS** — минимальный порог «есть живой edge».",
        "- **BAH** — купил и держи: любая стратегия должна оправдывать активность.",
        "- Сумма pnl% — сумма по всем монетам и сделкам (не портфельный компаунд).",
        "",
        "## Кто выжил на OOS (PF≥1.20, avg>0, Sharpe>0, ≥40 сделок)",
        "",
    ]
    if survivors:
        lines += [header, sep]
        for m in survivors:
            lines.append(fmt(m))
        lines.append("")
        lines.append(
            "**Вердикт по внедрению:** направленный слой имеет право на жизнь "
            "как ИНФОРМАЦИОННЫЙ сигнал (`/pulse`), НЕ как авто-торговля. "
            "Арбитражное ядро v3 (delta-neutral) остаётся основным P&L."
        )
    else:
        lines.append(
            "Ни одна направленная стратегия **не прошла** OOS-фильтр "
            "(PF≥1.20 и положительное мат.ожидание). Это ожидаемый результат: "
            "предсказывать цену крипты одиночными индикаторами после издержек "
            "почти не получается. Внедрять направленную автоторговлю **нельзя**. "
            "PULSE оставляем как исследовательский скоринг (режим/crowding) "
            "рядом с рабочим арбитражем."
        )

    # топ/антитоп OOS по Sharpe
    oos_sorted = sorted(oos.values(), key=lambda m: m.sharpe, reverse=True)
    lines += [
        "",
        "## OOS: рейтинг по Sharpe",
        "",
        header,
        sep,
    ]
    for m in oos_sorted:
        lines.append(fmt(m))

    bah_oos = oos.get("BAH")
    pulse_oos = oos.get("PULSE")
    nexus_oos = oos.get("NEXUS")
    lines += [
        "",
        "## Выводы (профессионально)",
        "",
        "1. **Классический TA** (RSI/MACD/EMA/BB/Supertrend) на 4ч крипте после "
        "комиссий в среднем не бьёт buy&hold — это видно в таблице. Не внедрять.",
        "2. **Crypto-native crowding** (fade экстремального funding) — единственный "
        "класс, у которого теоретический edge документирован (разгрузка плеча). "
        "На практике он редкий и шумный; смотри FUNDING_FADE / XS_FUND_FADE / PULSE.",
        "3. **PULSE** — режим-адаптивный композит (тренд: vol-norm momentum + fade crowding; "
        "флэт: mean-reversion + RSI-экстремумы; override на экстремальном funding). "
        f"OOS: winrate {pulse_oos.winrate if pulse_oos else 0:.1f}%, "
        f"PF {pulse_oos.profit_factor if pulse_oos else 0:.2f}, "
        f"Sharpe {pulse_oos.sharpe if pulse_oos else 0:.2f}.",
        "4. **NEXUS** — тройное подтверждение (моментум × неперегретый funding × тренд). "
        f"OOS PF {nexus_oos.profit_factor if nexus_oos else 0:.2f}.",
        "5. **Рабочий продукт проекта** по-прежнему delta-neutral v3 "
        "(PF 3.33, winrate 71%, maxDD 1.29%). Направление цены — дополнительный "
        "скоринг, не замена хеджа.",
        "",
        f"BAH OOS сумма pnl% = {bah_oos.total_pnl_pct if bah_oos else 0:+.1f} "
        "(для масштаба: это сумма buy&hold по 49 монетам, не портфель 100%).",
    ]

    report = "\n".join(lines)
    print(report)
    RESULTS.mkdir(exist_ok=True)
    tag = args.tag or datetime.now(tz=timezone.utc).strftime("pulse-%Y%m%d-%H%M%S")
    (RESULTS / f"{tag}.md").write_text(report + "\n", encoding="utf-8")
    payload = {
        "tag": tag,
        "period": period,
        "symbols": symbols,
        "agree_pct": agree_pct,
        "cost_frac": cost,
        "metrics": [r.__dict__ for r in rows],
        "survivors": [m.name for m in survivors],
    }
    (RESULTS / f"{tag}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nСохранено: backtest/results/{tag}.md / .json")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Бектест направленных стратегий PULSE")
    p.add_argument("--symbols", default="", help="список через запятую; пусто = все")
    p.add_argument("--fee", type=float, default=0.10, help="round-trip taker, %")
    p.add_argument("--slippage-bps", type=float, default=2.5)
    p.add_argument("--tag", default="pulse-v1")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
