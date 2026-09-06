from dataclasses import replace

from cryptopilot.config import Settings
from cryptopilot.indicators import compute_features
from cryptopilot.models import Side, Ticker
from cryptopilot.smart_money import _pre_move_score, _stage, _structure_label


def test_missing_spot_is_blocked_when_confirmation_required(candle_factory):
    feature = compute_features(candle_factory())
    ticker = Ticker("TESTUSDT", feature.close, feature.close, feature.close, 1e9, 1e6)
    settings = Settings(_env_file=None, prime_spot_confirmation_enabled=True)
    _, _, blockers = _pre_move_score(
        Side.LONG, feature, feature, feature, ticker, None, None, 0, settings
    )
    assert any("Spot-данные недоступны" in b for b in blockers)
    settings.prime_spot_confirmation_enabled = False
    _, _, blockers = _pre_move_score(
        Side.LONG, feature, feature, feature, ticker, None, None, 0, settings
    )
    assert not any("Spot-данные недоступны" in b for b in blockers)


def test_entry_needs_observed_taker_and_oi(candle_factory):
    f = compute_features(candle_factory())
    f = replace(
        f, breakout_up=True, relative_volume20=2, supertrend_direction=1, close=f.ema50 * 1.1
    )
    ticker = Ticker(
        "TESTUSDT",
        f.close,
        f.close,
        f.close,
        1e9,
        1e6,
        taker_buy_ratio=0.65,
        open_interest_change_pct=1,
    )
    assert _stage(Side.LONG, 90, f, f, ticker) == "ENTRY"
    assert _stage(Side.LONG, 90, f, f, replace(ticker, taker_buy_ratio=None)) != "ENTRY"
    assert _stage(Side.LONG, 90, f, f, replace(ticker, open_interest_change_pct=None)) != "ENTRY"


def test_ema_alignment_is_not_claimed_as_swing_structure(candle_factory):
    f = compute_features(candle_factory())
    f = replace(f, breakout_up=False, breakout_down=False, close=103, ema20=102, ema50=101)
    assert "экстремумы не проверены" in _structure_label(f)
    assert "HH/HL" not in _structure_label(f)
    assert "диапазона 20 свечей" in _structure_label(replace(f, breakout_up=True))
