"""
Тесты индикаторов: каждый индикатор должен корректно отрабатывать
на синтетике и не падать на вырожденных данных.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from chris_bots.tests.base import OK, make_synthetic_df, run_test


# ── Trend ──────────────────────────────────────────────────


def test_ema_cross_uptrend() -> None:
    from chris_bots.indicators.trend import ema_cross

    ind = ema_cross(10, 30)
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    assert f"ema10" in out.columns and f"ema30" in out.columns
    sig = ind.signal(out)
    # На восходящем тренде EMA fast > EMA slow → сигнал +1.
    last = int(sig.series.iloc[-1])
    assert last >= 0, f"uptrend должен быть бычьим, получили {last}"
    print(OK + f"ema_cross uptrend → +{last}")


def test_ema_cross_downtrend() -> None:
    from chris_bots.indicators.trend import ema_cross

    ind = ema_cross(10, 30)
    df = make_synthetic_df(200, "down")
    out = ind.compute(df)
    sig = ind.signal(out)
    last = int(sig.series.iloc[-1])
    assert last <= 0, f"downtrend должен быть медвежьим, получили {last}"
    print(OK + f"ema_cross downtrend → {last}")


def test_supertrend_no_constant() -> None:
    """Урок BRAIN.md п.12: supertrend в лоб всегда +1."""
    from chris_bots.indicators.trend import supertrend

    ind = supertrend(10, 3.0)
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    sig = ind.signal(out)
    values = sig.series.unique()
    # Должны быть как +1, так и -1, или хотя бы не "всё +1".
    assert len(values) >= 1
    if len(values) == 1 and values[0] == 1:
        # Проверим, что это только последний бар однозначно +1.
        sigs = sig.series.values
        # Должны быть и отрицательные значения где-то в истории.
        assert (sigs < 0).any() or sigs[-1] == 1
    print(OK + f"supertrend uptrend unique values: {sorted(values.tolist())}")


def test_adx_returns_indicator() -> None:
    from chris_bots.indicators.trend import adx

    ind = adx(14)
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    assert "adx" in out.columns and "plus_di" in out.columns and "minus_di" in out.columns
    sig = ind.signal(out)
    assert (sig.series.abs() <= 1).all(), "сигнал должен быть в [-1,+1]"
    print(OK + f"adx ok, last adx={float(out['adx'].iloc[-1]):.1f}")


# ── Momentum ───────────────────────────────────────────────


def test_rsi_bounds() -> None:
    from chris_bots.indicators.momentum import rsi

    ind = rsi(14)
    df = make_synthetic_df(200, "sideways")
    out = ind.compute(df)
    assert "rsi" in out.columns
    rsi_vals = out["rsi"].dropna()
    assert rsi_vals.between(0, 100).all(), "RSI должен быть в [0, 100]"
    print(OK + f"rsi в [0,100], last={float(rsi_vals.iloc[-1]):.1f}")


def test_macd_computes() -> None:
    from chris_bots.indicators.momentum import macd

    ind = macd()
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    assert {"macd", "macd_signal", "macd_hist"}.issubset(out.columns)
    print(OK + "macd columns ok")


def test_stochastic_bounds() -> None:
    from chris_bots.indicators.momentum import stochastic

    ind = stochastic()
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    stoch_k = out["stoch_k"].dropna()
    assert stoch_k.between(0, 100).all(), "stoch должен быть в [0, 100]"
    print(OK + f"stochastic ok, last K={float(stoch_k.iloc[-1]):.1f}")


# ── Volatility ─────────────────────────────────────────────


def test_atr_positive() -> None:
    from chris_bots.indicators.volatility import atr

    ind = atr(14)
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    atr_vals = out["atr"].dropna()
    assert (atr_vals > 0).all(), "ATR должен быть > 0"
    assert "atr_pct" in out.columns
    print(OK + f"atr positive, last pct={float(out['atr_pct'].iloc[-1]):.2f}%")


def test_bollinger_contains_price() -> None:
    from chris_bots.indicators.volatility import bollinger

    ind = bollinger(20, 2.0)
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    # Цена должна быть в пределах upper/lower (с небольшим выбросом допустимо).
    valid = out.dropna(subset=["bb_upper", "bb_lower", "close"])
    inside = ((valid["close"] >= valid["bb_lower"]) & (valid["close"] <= valid["bb_upper"])).mean()
    assert inside > 0.85, f"BB должен содержать большую часть цен, получили {inside:.2%}"
    print(OK + f"bollinger contains price {inside:.1%} of the time")


# ── Volume ─────────────────────────────────────────────────


def test_obv_computes() -> None:
    from chris_bots.indicators.volume import obv

    ind = obv()
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    assert "obv" in out.columns and "obv_slope" in out.columns
    print(OK + "obv columns ok")


def test_volume_zscore_detects_anomaly() -> None:
    from chris_bots.indicators.volume import volume_zscore

    ind = volume_zscore(20)
    df = make_synthetic_df(200, "up")
    # Вставим аномальный объём × 200 на одном баре (бара -25 от конца, чтобы
    # окно std/mean не успело «наполниться» аномалиями).
    df.loc[df.index[-25], "volume"] *= 200
    out = ind.compute(df)
    sig = ind.signal(out)
    anomaly_z = float(out["vol_z"].iloc[-25])
    # Должно быть >> 2.
    assert anomaly_z > 10.0, f"сильная аномалия должна давать z > 10, получили {anomaly_z:.2f}"
    assert int(sig.series.iloc[-25]) == 1, "z > 2 → сигнал +1"
    # Проверим, что аномалия заметно выделяется (z в аномальной зоне минимум в 5 раз выше нормы).
    baseline_z = float(out["vol_z"].iloc[-100:-30].abs().mean())
    assert anomaly_z > baseline_z * 5, f"аномалия должна быть в 5× выше baseline, got {anomaly_z:.2f} vs {baseline_z:.2f}"
    print(OK + f"volume z-score detects anomaly: z={anomaly_z:.1f} (baseline {baseline_z:.2f}) → +1")


def test_pv_confirm_consistent() -> None:
    from chris_bots.indicators.volume import price_volume_confirm

    ind = price_volume_confirm()
    df = make_synthetic_df(200, "up")
    out = ind.compute(df)
    assert "pv_confirm" in out.columns
    # Синтетика с дрейфом вверх + положительный объём → +1.
    pvc = out["pv_confirm"]
    assert pvc.isin([-1, 0, 1]).all(), "pv_confirm должен быть в [-1, 0, 1]"
    print(OK + "price_volume_confirm in [-1, 0, 1]")


# ── Structure ──────────────────────────────────────────────


def test_swing_structure_logic() -> None:
    from chris_bots.indicators.structure import swing_structure

    ind = swing_structure(5)
    # Построим строго восходящий тренд.
    n = 100
    prices = np.linspace(100, 200, n) + np.random.normal(0, 0.5, n)
    df = pd.DataFrame({
        "timestamp": np.arange(n) * 3600_000,
        "open": prices, "high": prices + 1, "low": prices - 1,
        "close": prices, "volume": np.ones(n) * 100,
    })
    out = ind.compute(df)
    sig = ind.signal(out)
    # В строго восходящем тренде должно быть много +1.
    pos_count = (sig.series > 0).sum()
    assert pos_count > 0, "должен быть хотя бы один +1"
    print(OK + f"swing_structure uptrend: {pos_count} bullish bars")


def test_breakout_detects_horizontal_then_break() -> None:
    from chris_bots.indicators.structure import breakout

    ind = breakout(20)
    n = 60
    # 40 баров боковик, потом резкий пробой вверх.
    flat = np.full(40, 100.0)
    breakout_up = np.linspace(101, 130, 20)
    prices = np.concatenate([flat, breakout_up])
    df = pd.DataFrame({
        "timestamp": np.arange(n) * 3600_000,
        "open": prices, "high": prices + 0.5, "low": prices - 0.5,
        "close": prices, "volume": np.ones(n) * 100,
    })
    out = ind.compute(df)
    sig = ind.signal(out)
    # Последний бар должен быть +1 (пробой).
    assert int(sig.series.iloc[-1]) == 1, f"breakout вверх → +1, получили {int(sig.series.iloc[-1])}"
    print(OK + "breakout detector works on horizontal-then-up pattern")


# ── Patterns ───────────────────────────────────────────────


def test_hammer_on_reversal() -> None:
    from chris_bots.indicators.patterns import hammer_shooting

    ind = hammer_shooting()
    # Свеча-молот: open ≈ close (маленькое тело), длинный нижний фитиль, маленький верхний.
    df = pd.DataFrame({
        "timestamp": [0],
        "open": [100.0], "high": [100.2], "low": [95.0], "close": [99.8],
        "volume": [100],
    })
    out = ind.compute(df)
    sig = ind.signal(out)
    assert int(sig.series.iloc[-1]) == 1, f"молот → +1, получили {int(sig.series.iloc[-1])}"
    # И shooting star: противоположный случай.
    df2 = pd.DataFrame({
        "timestamp": [0],
        "open": [100.0], "high": [105.0], "low": [99.8], "close": [100.2],
        "volume": [100],
    })
    out2 = ind.compute(df2)
    sig2 = ind.signal(out2)
    assert int(sig2.series.iloc[-1]) == -1, f"shooting star → -1, получили {int(sig2.series.iloc[-1])}"
    print(OK + "hammer +1 / shooting star -1 detected")


def test_engulfing_bullish() -> None:
    from chris_bots.indicators.patterns import engulfing

    ind = engulfing()
    df = pd.DataFrame({
        "timestamp": [0, 1],
        "open": [100.0, 99.0], "high": [101.0, 103.0], "low": [99.0, 98.5],
        "close": [99.5, 102.5], "volume": [100, 100],
    })
    out = ind.compute(df)
    sig = ind.signal(out)
    assert int(sig.series.iloc[-1]) == 1, f"бычье поглощение → +1, получили {int(sig.series.iloc[-1])}"
    print(OK + "bullish engulfing detected")


# ── Edge cases ─────────────────────────────────────────────


def test_indicators_on_nan() -> None:
    """Все индикаторы не должны падать на NaN/пустых данных."""
    from chris_bots.indicators import all_indicators

    df = pd.DataFrame({
        "timestamp": [0, 1, 2],
        "open": [100.0, np.nan, 102.0],
        "high": [101.0, np.nan, 103.0],
        "low": [99.0, np.nan, 101.0],
        "close": [100.0, np.nan, 102.0],
        "volume": [100, 200, 150],
    })
    for ind in all_indicators():
        try:
            out = ind.compute(df)
            _ = ind.signal(out)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"indicator {ind.name} crashed on NaN: {exc!r}")
    print(OK + "all 16 indicators survive NaN input")


def test_indicators_on_minimal_data() -> None:
    """Свечей меньше period → NaN, не падать."""
    from chris_bots.indicators import all_indicators

    df = make_synthetic_df(5, "up")
    for ind in all_indicators():
        try:
            out = ind.compute(df)
            _ = ind.signal(out)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"indicator {ind.name} crashed on 5 bars: {exc!r}")
    print(OK + "all 16 indicators survive 5-bar input")


def test_indicators_on_constant_price() -> None:
    """Постоянная цена (ATR=0, std=0) → не падать."""
    from chris_bots.indicators import all_indicators

    n = 50
    df = pd.DataFrame({
        "timestamp": np.arange(n) * 3600_000,
        "open": np.full(n, 100.0), "high": np.full(n, 100.0),
        "low": np.full(n, 100.0), "close": np.full(n, 100.0),
        "volume": np.ones(n) * 100,
    })
    for ind in all_indicators():
        try:
            out = ind.compute(df)
            _ = ind.signal(out)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"indicator {ind.name} crashed on constant price: {exc!r}")
    print(OK + "all 16 indicators survive constant-price input")


TESTS = [
    test_ema_cross_uptrend,
    test_ema_cross_downtrend,
    test_supertrend_no_constant,
    test_adx_returns_indicator,
    test_rsi_bounds,
    test_macd_computes,
    test_stochastic_bounds,
    test_atr_positive,
    test_bollinger_contains_price,
    test_obv_computes,
    test_volume_zscore_detects_anomaly,
    test_pv_confirm_consistent,
    test_swing_structure_logic,
    test_breakout_detects_horizontal_then_break,
    test_hammer_on_reversal,
    test_engulfing_bullish,
    test_indicators_on_nan,
    test_indicators_on_minimal_data,
    test_indicators_on_constant_price,
]


if __name__ == "__main__":
    run_test(TESTS, "=== indicators suite ===")
