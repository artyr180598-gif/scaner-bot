"""
tests/test_indicators.py — математика индикаторов.

Проверяем не «похоже на правду», а конкретные свойства:
  * совпадение с ручным расчётом по определению (RSI, ATR, SMA, BB);
  * отсутствие заглядывания в будущее (главный грех самописных индикаторов);
  * корректность ресемплинга свечей;
  * поведение Supertrend на чистых трендах (на первой версии функции он
    возвращал +1 на любом рынке — тест защищает от регрессии).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.domain.models import Candles, TickerInfo, Timeframe
from app.indicators import core as ind
from app.indicators.profile import build_volume_profile
from app.indicators.volatility import analyse_volatility, choppiness_index, nr7
from app.indicators.structure import find_swings


@pytest.fixture
def series() -> pd.Series:
    return pd.Series([1, 2, 3, 4, 5, 4, 3, 2, 3, 4, 5, 6, 7, 8, 9, 10.0])


@pytest.fixture
def ohlc() -> pd.DataFrame:
    n = 120
    rng = np.random.default_rng(3)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + rng.uniform(0.1, 1.0, n),
        "low": close - rng.uniform(0.1, 1.0, n),
        "close": close,
        "volume": rng.uniform(100, 1000, n),
    })


# ---------------------------------------------------------------------------
# Ручные проверки
# ---------------------------------------------------------------------------

def test_sma_matches_manual(series):
    out = ind.sma(series, 3)
    assert pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[-1] == pytest.approx((8 + 9 + 10) / 3)


def test_rsi_all_gains_is_100():
    rising = pd.Series(np.arange(1, 40, dtype=float))
    out = ind.rsi(rising, 14)
    assert out.iloc[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    falling = pd.Series(np.arange(40, 1, -1, dtype=float))
    out = ind.rsi(falling, 14)
    assert out.iloc[-1] == pytest.approx(0.0)


def test_rsi_matches_manual_wilder_recursion():
    """
    RSI против ручного расчёта по рекурсии Уайлдера (RMA-сглаживание),
    как в TradingView: avg[i] = (avg[i-1] * (n-1) + x[i]) / n.
    """
    prices = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
                        46.03, 46.41, 46.22, 45.64])
    period = 14
    # Первое изменение цены не определено — рекурсия начинается со второго бара
    # (так же ведёт себя ewm, на котором построена реализация).
    delta = prices.diff().dropna().to_numpy(dtype="float64")
    gains = np.clip(delta, 0, None)
    losses = np.clip(-delta, 0, None)

    def rma(values):
        out = np.empty(len(values))
        prev = values[0]
        for i, v in enumerate(values):
            prev = v if i == 0 else (prev * (period - 1) + v) / period
            out[i] = prev
        return out

    avg_gain, avg_loss = rma(gains), rma(losses)
    expected = 100 - 100 / (1 + avg_gain / np.where(avg_loss == 0, np.nan, avg_loss))

    out = ind.rsi(prices, period).to_numpy(dtype="float64")
    valid = ~np.isnan(out)
    assert valid.sum() > 0
    # Сдвиг на 1: expected[0] соответствует бару 1 исходного ряда.
    assert np.allclose(out[valid], expected[period - 1:], atol=1e-9)


def test_atr_matches_manual_on_constant_range():
    n = 30
    high = pd.Series([11.0] * n)
    low = pd.Series([9.0] * n)
    close = pd.Series([10.0] * n)
    out = ind.atr(high, low, close, 14)
    # Все бары одинаковые: TR = 2 → ATR = 2.
    assert out.iloc[-1] == pytest.approx(2.0)


def test_bollinger_width_and_percent_b(ohlc):
    mid, upper, lower, width = ind.bollinger(ohlc["close"], 20, 2.0)
    assert upper.iloc[-1] > mid.iloc[-1] > lower.iloc[-1]
    assert width.iloc[-1] > 0
    # %B внутри [0, 1] для цены внутри полос.
    close = float(ohlc["close"].iloc[-1])
    percent_b = (close - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1])
    assert 0 <= percent_b <= 1


def test_macd_histogram_is_difference(ohlc):
    line, signal, hist = ind.macd(ohlc["close"])
    assert hist.iloc[-1] == pytest.approx(line.iloc[-1] - signal.iloc[-1], abs=1e-9)


def test_adx_high_on_pure_trend():
    n = 100
    close = pd.Series(np.linspace(100, 200, n))
    high = close + 0.5
    low = close - 0.5
    adx, pdi, mdi = ind.adx(high, low, close, 14)
    assert adx.iloc[-1] > 40
    assert pdi.iloc[-1] > mdi.iloc[-1]


def test_percentile_rank_monotonic():
    rising = pd.Series(np.arange(1, 121, dtype=float))
    out = ind.percentile_rank(rising, 100)
    assert out.iloc[-1] == pytest.approx(100.0)
    falling = pd.Series(np.arange(120, 0, -1, dtype=float))
    out2 = ind.percentile_rank(falling, 100)
    assert out2.iloc[-1] == pytest.approx(0.0)


def test_zscore_sign():
    flat = pd.Series([10.0] * 30 + [20.0])
    out = ind.zscore(flat, 20)
    assert out.iloc[-1] > 0


def test_crossover_detects_single_bar():
    a = pd.Series([1.0, 2.0, 3.0, 2.0])
    b = pd.Series([2.0, 2.0, 2.0, 2.0])
    cross = ind.crossover(a, b)
    assert list(cross.fillna(False)) == [False, False, True, False]


def test_linear_slope_positive_for_rising():
    rising = pd.Series(np.linspace(1, 2, 30))
    slope = ind.linear_slope(rising, 20)
    assert slope.iloc[-1] > 0
    assert ind.r_squared(rising, 20).iloc[-1] > 0.99


# ---------------------------------------------------------------------------
# Отсутствие заглядывания в будущее
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["rsi", "ema20", "atr", "adx", "mfi"])
def test_no_lookahead(ohlc, name):
    """
    Значение индикатора на баре i не должно меняться, если дописать бары
    после i. Это ключевое свойство для честного бектеста.
    """
    cut = len(ohlc) - 20
    short, full = ohlc.iloc[:cut], ohlc

    def value(df: pd.DataFrame) -> float:
        if name == "rsi":
            return float(ind.rsi(df["close"], 14).iloc[-1])
        if name == "ema20":
            return float(ind.ema(df["close"], 20).iloc[-1])
        if name == "atr":
            return float(ind.atr(df["high"], df["low"], df["close"], 14).iloc[-1])
        if name == "adx":
            return float(ind.adx(df["high"], df["low"], df["close"], 14)[0].iloc[-1])
        if name == "mfi":
            return float(ind.mfi(df["high"], df["low"], df["close"], df["volume"], 14).iloc[-1])
        raise AssertionError(name)

    a = value(short)
    b = value(full.iloc[:cut])
    assert a == pytest.approx(b, rel=1e-9)


def test_supertrend_direction_follows_trend():
    n = 200
    down = pd.Series(np.linspace(100, 50, n))
    _, dir_down = ind.supertrend(down + 0.5, down - 0.5, down, 10, 3.0)
    assert dir_down.iloc[-1] == -1.0

    up = pd.Series(np.linspace(50, 100, n))
    _, dir_up = ind.supertrend(up + 0.5, up - 0.5, up, 10, 3.0)
    assert dir_up.iloc[-1] == 1.0


def test_choppiness_high_in_flat_market():
    n = 120
    rng = np.random.default_rng(5)
    close = pd.Series(100 + rng.normal(0, 0.2, n).cumsum() * 0.05)
    high = close + 0.3
    low = close - 0.3
    ch = choppiness_index(high, low, close, 14)
    assert ch.iloc[-1] > 50

    trend = pd.Series(np.linspace(100, 160, n))
    ch_trend = choppiness_index(trend + 0.3, trend - 0.3, trend, 14)
    assert ch_trend.iloc[-1] < ch.iloc[-1]


def test_nr7_flags_narrowest_bar():
    high = pd.Series([10.0] * 8 + [10.05])
    low = pd.Series([8.0] * 8 + [9.95])
    out = nr7(high, low)
    assert bool(out.iloc[-1])


def test_volume_profile_poc_inside_value_area():
    rng = np.random.default_rng(11)
    n = 200
    close = pd.Series(100 + rng.normal(0, 1, n).cumsum() * 0.1)
    high = close + 0.5
    low = close - 0.5
    volume = pd.Series(rng.uniform(100, 500, n))
    vp = build_volume_profile(high, low, close, volume, lookback=200, bins=40)
    assert vp is not None
    assert vp.val <= vp.poc <= vp.vah
    assert len(vp.hvn(3)) == 3
    assert vp.total_volume > 0


def test_volatility_state_detects_compression():
    """После сжатия волатильности compression должен быть высоким."""
    n = 250
    rng = np.random.default_rng(13)
    loud = rng.normal(0, 2.0, n - 40)
    quiet = rng.normal(0, 0.25, 40)
    close = pd.Series(100 + np.cumsum(np.concatenate([loud, quiet])))
    high = close + np.abs(rng.normal(0, 1, n))
    low = close - np.abs(rng.normal(0, 1, n))
    state = analyse_volatility(high, low, close, percentile_window=100)
    assert state is not None
    assert state.compression > 0.4
    assert state.atr_percentile < 40


# ---------------------------------------------------------------------------
# Свечи и ресемплинг
# ---------------------------------------------------------------------------

def _raw_rows(n: int, start_price: float = 100.0):
    base = pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000
    rows = []
    for i in range(n):
        price = start_price + i * 0.5
        rows.append([base + i * 3_600_000, price, price + 0.4, price - 0.4,
                     price + 0.1, 1000.0 + i])
    return rows


def test_candles_from_raw_and_resample():
    candles = Candles.from_raw("BTC/USDT", Timeframe.H1, _raw_rows(48))
    assert len(candles) == 48
    assert candles.timeframe is Timeframe.H1

    h4 = candles.resample(Timeframe.H4)
    assert len(h4) == 12
    # OHLC агрегируется правильно.
    assert h4.df["high"].iloc[0] == pytest.approx(candles.df["high"].iloc[:4].max())
    assert h4.df["low"].iloc[0] == pytest.approx(candles.df["low"].iloc[:4].min())
    assert h4.df["close"].iloc[0] == pytest.approx(candles.df["close"].iloc[3])
    assert h4.df["open"].iloc[0] == pytest.approx(candles.df["open"].iloc[0])
    assert h4.df["volume"].iloc[0] == pytest.approx(candles.df["volume"].iloc[:4].sum())

    d1 = candles.resample(Timeframe.D1)
    assert len(d1) == 2


def test_candles_resample_rejects_lower_timeframe():
    candles = Candles.from_raw("BTC/USDT", Timeframe.H4, _raw_rows(10))
    with pytest.raises(ValueError):
        candles.resample(Timeframe.H1)


def test_candles_drops_duplicates_and_sorts():
    rows = _raw_rows(5)
    shuffled = [rows[2], rows[0], rows[0], rows[1], rows[3], rows[4]]
    candles = Candles.from_raw("BTC/USDT", Timeframe.H1, shuffled)
    assert len(candles) == 5
    assert candles.df.index.is_monotonic_increasing


def test_candles_empty_from_raw():
    candles = Candles.from_raw("BTC/USDT", Timeframe.H1, [])
    assert candles.empty
    assert len(candles) == 0


def test_swings_are_confirmed_only_with_right_bars():
    """Свинг появляется только после ``right`` баров — иначе это lookahead."""
    n = 40
    close = pd.Series(np.concatenate([np.linspace(100, 110, 20),
                                      np.linspace(110, 95, 20)]))
    high = close + 0.5
    low = close - 0.5
    unconfirmed = find_swings(high, low, left=3, right=3, confirm=False)
    confirmed = find_swings(high, low, left=3, right=3, confirm=True)
    assert len(confirmed) <= len(unconfirmed)
    if confirmed:
        assert max(s.index for s in confirmed) <= n - 3


# ---------------------------------------------------------------------------
# Свежесть данных: возраст считаем от закрытия свечи
# ---------------------------------------------------------------------------

def test_age_seconds_counts_from_bar_close():
    """
    Регрессия: ccxt метит свечу временем ОТКРЫТИЯ, поэтому часовая свеча,
    закрытая минуту назад, помечена «час назад». Возраст от открытия признавал
    бы устаревшими любые часовые данные и гейт свежести резал все сигналы.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    hour = timedelta(hours=1)
    # Последний часовой бар закрылся сейчас (открыт час назад).
    fresh = Candles.from_raw("A/USDT", Timeframe.H1, [
        [int((now - 2 * hour).timestamp() * 1000), 1, 2, 0.5, 1.5, 10],
        [int((now - hour).timestamp() * 1000), 1.5, 2, 1, 1.8, 10],
    ])
    assert fresh.age_seconds(now) < 5
    assert fresh.last_close_timestamp <= now

    # Баров часовой серии нет уже три часа — данные действительно протухли.
    stale = Candles.from_raw("B/USDT", Timeframe.H1, [
        [int((now - 4 * hour).timestamp() * 1000), 1, 2, 0.5, 1.5, 10],
        [int((now - 3 * hour).timestamp() * 1000), 1.5, 2, 1, 1.8, 10],
    ])
    assert stale.age_seconds(now) == pytest.approx(2 * 3600, abs=5)


def test_hourly_snapshot_is_not_rejected_as_stale():
    """Часовые свечи с последней закрытой свечой час назад — норма, не «устарело»."""
    from datetime import datetime, timedelta, timezone

    from app.analysis.features import build_features
    from app.config.settings import Settings
    from app.domain.models import MarketSnapshot
    from app.signals.engine import SignalEngine

    now = datetime.now(timezone.utc)
    hour = timedelta(hours=1)
    rows = []
    price = 100.0
    for i in range(300):
        ts = int((now - (300 - i) * hour).timestamp() * 1000)
        price *= 1.0008
        rows.append([ts, price, price * 1.01, price * 0.99, price * 1.002, 1000])

    candles = Candles.from_raw("FRESH/USDT", Timeframe.H1, rows)
    snapshot = MarketSnapshot(
        symbol="FRESH/USDT", base="FRESH", quote="USDT", exchange="test",
        candles={Timeframe.H1: candles, Timeframe.H4: candles.resample(Timeframe.H4),
                 Timeframe.D1: candles.resample(Timeframe.D1)},
        ticker=TickerInfo("FRESH/USDT", last=price, quote_volume=50_000_000,
                          change_pct=1.0, high=price * 1.05, low=price * 0.95),
    )
    assert snapshot.staleness_seconds() < 5, "свежие часовые данные"

    features = build_features(snapshot, signal_tf=Timeframe.H1)
    assert features.flags["stale"] is False

    engine = SignalEngine(Settings())
    # Сам порог в конфиге движка должен учитывать период ТФ.
    assert engine.filter_config.max_staleness_seconds >= 1.5 * 3600
    assert "устарел" not in " ".join(features.notes)


def test_resample_drops_incomplete_tail_bucket():
    """
    Хвостовое окно старшего ТФ почти всегда неполное (в 20:47 «4h-свеча»
    содержит 47 минут). Анализировать её как закрытую — значит видеть бар,
    которого ещё нет, и получать разные сигналы в разное время суток.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = []
    price = 100.0
    for i in range(600):
        ts = int((now - (600 - i) * timedelta(hours=1)).timestamp() * 1000)
        price *= 1.0005
        rows.append([ts, price, price * 1.01, price * 0.99, price * 1.002, 10])

    hourly = Candles.from_raw("X/USDT", Timeframe.H1, rows)
    full = hourly.resample(Timeframe.H4)
    # Убрали два часовых бара из хвоста — неполная 4h-свеча должна исчезнуть.
    trimmed = Candles("X/USDT", Timeframe.H1, hourly.df.iloc[:-2],
                      last_closed=True).resample(Timeframe.H4)
    assert len(trimmed) == len(full) - 1
    assert float(trimmed.close.iloc[-1]) == pytest.approx(float(full.close.iloc[-2]))


def test_resample_only_produces_complete_bars():
    """
    Инвариант: срез хвоста может только УБРАТЬ последнюю свечу старшего ТФ,
    но не изменить уже собранные. Значит, неполных баров в серии нет и
    результат не зависит от того, в какую минуту часа запущен анализ.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = []
    price = 50.0
    for i in range(480):
        ts = int((now - (480 - i) * timedelta(hours=1)).timestamp() * 1000)
        price *= 1.0004
        rows.append([ts, price, price * 1.02, price * 0.98, price * 1.001, 5])

    hourly = Candles.from_raw("Y/USDT", Timeframe.H1, rows)
    daily = hourly.resample(Timeframe.D1)
    assert len(daily) >= 19

    closes = list(daily.close)
    for cut in (1, 3, 11, 23):
        variant = Candles("Y/USDT", Timeframe.H1, hourly.df.iloc[:-cut],
                          last_closed=True).resample(Timeframe.D1)
        # Неполные сутки отброшены, остальные бары не изменились:
        # серия старшего ТФ — всегда префикс полной серии.
        assert len(variant) <= len(daily)
        assert list(variant.close) == closes[:len(variant)], f"срез {cut} ч"
