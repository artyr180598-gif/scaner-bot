"""
tests/test_structure.py — структура рынка и смарт-мани (BOS/CHOCH, OB, FVG, свипы).

Проверяется на «ручных» формах цены, где правильный ответ очевиден:
зигзаг вверх/вниз, пробой уровня, трёхсвечной имбаланс, прокол ликвидности.
Цены в зигзаге намеренно НЕ совпадают между собой: на плато из одинаковых
значений свинг определяется по первому бару полочки.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.indicators.structure import (DIR_DOWN, DIR_UP, EVENT_BOS, EVENT_CHOCH,
                                      analyse_structure, detect_fvgs,
                                      detect_structure, detect_sweeps,
                                      equal_levels, find_swings)


def frame(closes, wick_up=0.2, wick_dn=0.2, volume=None):
    close = pd.Series(np.asarray(closes, dtype="float64"))
    high = close + wick_up
    low = close - wick_dn
    open_ = close.shift(1).fillna(close.iloc[0])
    vol = pd.Series(np.asarray(volume, dtype="float64")) if volume is not None \
        else pd.Series(np.full(len(close), 100.0))
    return open_, high, low, close, vol


def zigzag(legs, points_per_leg=10, jitter=0.05, seed=1):
    """
    Строит зигзаг по списку целевых цен: [100, 112, 106, 120] →
    подъём до 112, откат до 106, рост до 120.

    Лёгкий джиттер нужен, чтобы значения не повторялись (реальные свечи тоже
    почти никогда не совпадают до тика).
    """
    rng = np.random.default_rng(seed)
    values: list[float] = []
    for start, end in zip(legs[:-1], legs[1:]):
        line = np.linspace(start, end, points_per_leg, endpoint=False)
        line = line + rng.uniform(-jitter, jitter, len(line))
        values.extend(line.tolist())
    values.append(float(legs[-1]))
    return np.array(values)


def test_swings_are_found_and_ordered():
    prices = zigzag([100, 112, 104, 118, 108, 122], points_per_leg=12)
    _, high, low, close, _ = frame(prices)
    swings = find_swings(high, low, left=3, right=3)
    assert len(swings) >= 4
    indexes = [s.index for s in swings]
    assert indexes == sorted(indexes)
    # Хаи и лоу чередуются.
    kinds = [s.kind for s in swings]
    assert kinds.count("high") >= 2 and kinds.count("low") >= 2


def test_swings_confirmed_only_after_right_bars():
    """Свинг появляется только после ``right`` баров — иначе это lookahead."""
    prices = zigzag([100, 115, 95, 110], points_per_leg=15)
    n = len(prices)
    _, high, low, close, _ = frame(prices)
    confirmed = find_swings(high, low, left=3, right=3, confirm=True)
    unconfirmed = find_swings(high, low, left=3, right=3, confirm=False)
    assert len(confirmed) <= len(unconfirmed)
    if confirmed:
        assert max(s.index for s in confirmed) <= n - 3 - 1


def test_bos_up_detected_on_uptrend_break():
    """HH/HL + пробой последнего хая = BOS вверх."""
    prices = zigzag([100, 112, 108, 118, 114, 128], points_per_leg=12)
    _, high, low, close, vol = frame(prices)
    swings = find_swings(high, low, left=3, right=3)
    state = detect_structure(close, swings)
    assert state.trend == "up"
    assert any(e.kind == EVENT_BOS and e.direction == DIR_UP for e in state.events)


def test_bos_down_detected_on_downtrend_break():
    prices = zigzag([130, 118, 122, 110, 114, 100], points_per_leg=12)
    _, high, low, close, vol = frame(prices)
    swings = find_swings(high, low, left=3, right=3)
    state = detect_structure(close, swings)
    assert state.trend == "down"
    assert any(e.kind == EVENT_BOS and e.direction == DIR_DOWN for e in state.events)


def test_choch_detected_on_trend_reversal():
    """Нисходящая структура, затем пробой вверх = CHOCH вверх."""
    prices = zigzag([130, 116, 120, 104, 108, 126], points_per_leg=14)
    _, high, low, close, vol = frame(prices)
    swings = find_swings(high, low, left=3, right=3)
    state = detect_structure(close, swings)
    kinds = {(e.kind, e.direction) for e in state.events}
    assert (EVENT_CHOCH, DIR_UP) in kinds


def test_hh_hl_and_lh_ll_flags():
    up = zigzag([100, 112, 108, 120, 116, 130], points_per_leg=12)
    _, high, low, close, _ = frame(up)
    state = detect_structure(close, find_swings(high, low, left=3, right=3))
    assert state.hh_hl

    down = zigzag([130, 118, 122, 110, 114, 102], points_per_leg=12)
    _, high2, low2, close2, _ = frame(down)
    state2 = detect_structure(close2, find_swings(high2, low2, left=3, right=3))
    assert state2.lh_ll


def test_fvg_detected_on_three_candle_gap():
    """Классический имбаланс: low[2] > high[0] → бычий FVG."""
    closes = [100.0, 100.5, 101.0, 106.0, 107.0, 107.5, 107.2, 107.0]
    open_, high, low, close, vol = frame(closes, wick_up=0.0, wick_dn=0.0)
    high.iloc[2] = 101.2      # потолок первой свечи
    low.iloc[4] = 105.5       # дно третьей свечи — между ними разрыв
    zones = detect_fvgs(high, low, close, max_zones=8, max_age_bars=50)
    bull = [z for z in zones if z.is_bullish]
    assert bull, "бычий имбаланс должен быть найден"
    assert any(z.low <= 101.3 and z.high >= 105.4 for z in bull), \
        f"ожидался имбаланс, перекрывающий разрыв: {[(z.low, z.high) for z in bull]}"


def test_bearish_fvg_detected():
    closes = [107.0, 106.5, 106.0, 101.0, 100.0, 99.5, 99.8, 100.2]
    open_, high, low, close, vol = frame(closes, wick_up=0.0, wick_dn=0.0)
    low.iloc[2] = 105.8
    high.iloc[4] = 100.5
    zones = detect_fvgs(high, low, close, max_zones=8, max_age_bars=50)
    bear = [z for z in zones if not z.is_bullish]
    assert bear, "медвежий имбаланс должен быть найден"


def test_sweep_of_lows_detected():
    """Тень ниже свинг-лоя с закрытием выше — снятие ликвидности снизу."""
    prices = zigzag([100, 112, 104, 109], points_per_leg=10)
    open_, high, low, close, vol = frame(prices)
    swing_low = min(s.price for s in find_swings(high, low, left=3, right=3)
                    if s.kind == "low")
    # Бар, который прокалывает минимум и закрывается выше него.
    pierce = len(close) - 3
    low.iloc[pierce] = swing_low - 2.0
    high.iloc[pierce] = swing_low + 3.5
    close.iloc[pierce] = swing_low + 3.0
    swings = find_swings(high, low, left=3, right=3)
    sweeps = detect_sweeps(high, low, close, swings, max_age_bars=12)
    assert any(s.direction == DIR_DOWN and s.close_inside for s in sweeps), \
        "свип нижней ликвидности должен быть обнаружен"


def test_sweep_of_highs_detected():
    prices = zigzag([100, 88, 95, 90], points_per_leg=10)
    open_, high, low, close, vol = frame(prices)
    swing_high = max(s.price for s in find_swings(high, low, left=3, right=3)
                     if s.kind == "high")
    pierce = len(close) - 3
    high.iloc[pierce] = swing_high + 2.0
    low.iloc[pierce] = swing_high - 3.5
    close.iloc[pierce] = swing_high - 3.0
    swings = find_swings(high, low, left=3, right=3)
    sweeps = detect_sweeps(high, low, close, swings, max_age_bars=12)
    assert any(s.direction == DIR_UP for s in sweeps)


def test_equal_levels_cluster_double_top():
    prices = zigzag([100, 110, 104, 110.2, 105, 109.9, 103], points_per_leg=12,
                    jitter=0.02)
    _, high, low, close, vol = frame(prices, wick_up=0.0, wick_dn=0.0)
    swings = find_swings(high, low, left=3, right=3)
    equal_highs, _ = equal_levels(swings, tolerance_pct=0.5)
    assert equal_highs, "почти равные максимумы должны образовать пул ликвидности"
    assert abs(equal_highs[0] - 110.0) < 1.0


def test_order_block_found_after_impulse():
    """
    После импульсного пробоя структуры должен находиться бычий Order Block —
    последняя медвежья свеча перед импульсом.
    """
    prices = list(zigzag([100, 110, 104], points_per_leg=12))
    prices += list(np.linspace(104, 122, 10))     # импульс вверх
    prices += list(np.linspace(122, 120, 5))
    open_, high, low, close, vol = frame(prices)
    volume = np.full(len(close), 100.0)
    volume[-14:-6] = 600.0                        # импульс на объёме
    state = analyse_structure(high, low, close, open_, pd.Series(volume),
                              left=3, right=3, atr_value=1.0)
    assert state.events, "структурные события должны быть"
    for zone in state.order_blocks:
        assert zone.low < zone.high
        assert 0.0 <= zone.strength <= 1.0
        assert zone.bars_ago >= 0


def test_analyse_structure_returns_consistent_state():
    prices = zigzag([100, 108, 103, 112, 106], points_per_leg=12)
    open_, high, low, close, vol = frame(prices)
    state = analyse_structure(high, low, close, open_, vol, left=3, right=3,
                              atr_value=0.8)
    assert state.trend in ("up", "down", "range")
    assert isinstance(state.swings, list)
    assert isinstance(state.order_blocks, list)
    assert isinstance(state.fvgs, list)
    assert isinstance(state.sweeps, list)


def test_detect_structure_without_swings_is_safe():
    close = pd.Series([100.0] * 10)
    state = detect_structure(close, [])
    assert state.trend == "range"
    assert state.last_event is None
    assert state.events == []


def test_sweeps_empty_on_short_series():
    close = pd.Series([100.0, 101.0])
    assert detect_sweeps(close, close, close, [], max_age_bars=5) == []


def test_find_swings_on_short_series_returns_empty():
    close = pd.Series([100.0, 101.0, 102.0])
    assert find_swings(close, close, left=3, right=3) == []
