"""
tests/test_render.py — рендеринг интерфейса.

Интерфейс — это то, что видит пользователь, поэтому проверяем не «не упало»,
а наличие обязательных блоков: направление, уверенность, вход, стоп, цели,
R:R, объяснение и дисклеймер. Плюс экранирование HTML.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.data.synthetic import make_snapshot
from app.domain.models import (Direction, MarketContext, ScanReport, Signal,
                               Timeframe)
from app.presentation import render
from app.presentation.format import (ago, base_of, fmt_pct, fmt_price, fmt_ratio,
                                     fmt_usd, plural, progress_bar)
from app.signals.engine import SignalEngine


@pytest.fixture
def long_signal() -> Signal:
    settings = Settings()
    settings.min_confidence = 4.0
    settings.min_rr = 1.1
    engine = SignalEngine(settings)
    snapshot = make_snapshot("TEST/USDT", "breakout", seed=21, bars=520)
    signal = engine.analyze(snapshot, MarketContext(btc_score=0.4, btc_trend="восходящий"))
    assert signal.actionable, "для теста карточки нужен активный сигнал"
    return signal


@pytest.fixture
def wait_signal() -> Signal:
    settings = Settings()
    settings.min_confidence = 9.9        # гарантированно не пройдёт гейт
    engine = SignalEngine(settings)
    snapshot = make_snapshot("TEST/USDT", "range", seed=3, bars=400)
    return engine.analyze(snapshot, MarketContext())


# ---------------------------------------------------------------------------
# Форматирование чисел
# ---------------------------------------------------------------------------

def test_price_precision_adapts_to_magnitude():
    assert fmt_price(67420.5) == "67,420.5"
    assert fmt_price(1234.5678).startswith("1,234.5")
    assert fmt_price(0.123456) == "0.123456"
    # Для микро-цен: 8 знаков по умолчанию, лишние нули отбрасываются.
    assert fmt_price(0.000001234) == "0.00000123"
    assert fmt_price(0.000001234, max_digits=12) == "0.000001234"
    assert fmt_price(float("nan")) == "—"
    assert fmt_price(None) == "—"


def test_percent_and_ratio_format():
    assert fmt_pct(3.14159) == "+3.14%"
    assert fmt_pct(-2.0, digits=1) == "-2.0%"
    assert fmt_ratio(3.2) == "1:3.2"
    assert fmt_ratio(float("nan")) == "—"


def test_usd_format_compact():
    assert fmt_usd(25_000_000) == "$25.00M"
    assert fmt_usd(45_000) == "$45.0K"
    assert fmt_usd(999) == "$999"


def test_progress_bar_length_and_fill():
    bar = progress_bar(0.5, 10)
    assert len(bar) == 10
    assert bar.count("█") == 5
    assert progress_bar(1.5, 10) == "█" * 10
    assert progress_bar(-1, 10) == "░" * 10


def test_plural_russian_forms():
    assert plural(1, "монета", "монеты", "монет") == "монета"
    assert plural(2, "монета", "монеты", "монет") == "монеты"
    assert plural(5, "монета", "монеты", "монет") == "монет"
    assert plural(11, "монета", "монеты", "монет") == "монет"
    assert plural(21, "монета", "монеты", "монет") == "монета"


def test_ago_and_base_of():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert "мин назад" in ago(now - timedelta(minutes=5), now)
    assert "ч назад" in ago(now - timedelta(hours=3), now)
    assert ago(None) == "—"
    assert base_of("BTC/USDT") == "BTC"


# ---------------------------------------------------------------------------
# Карточка сигнала
# ---------------------------------------------------------------------------

def test_signal_card_contains_all_required_blocks(long_signal):
    text = render.render_signal(long_signal, deposit=1000.0)
    assert "LONG" in text
    assert "Уверенность" in text
    assert "Вход:" in text
    assert "Стоп:" in text
    assert "TP1" in text and "TP2" in text and "TP3" in text
    assert "R:R" in text
    assert "Почему именно эта монета" in text
    assert "Не является" in text          # дисклеймер
    assert "$TEST" in text


def test_signal_card_uses_html_markup(long_signal):
    text = render.render_signal(long_signal)
    assert "<b>" in text and "</b>" in text


def test_signal_card_escapes_user_text(long_signal):
    long_signal.setup = "<script>alert(1)</script>"
    text = render.render_signal(long_signal)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_wait_card_explains_reason(wait_signal):
    text = render.render_signal(wait_signal)
    assert "ЖДЁМ" in text
    assert "сигнала нет" in text.lower() or "нет" in text
    assert "Почему не сигнал" in text


def test_deep_analysis_shows_group_scores(long_signal):
    text = render.render_deep_analysis(long_signal, deposit=1000.0)
    assert "Глубокий анализ" in text
    assert "Тренд" in text
    assert "Импульс" in text
    assert "Потенциал" in text
    assert "Главные аргументы" in text
    assert "Метрики" in text


def test_deep_analysis_of_wait_signal_is_safe(wait_signal):
    text = render.render_deep_analysis(wait_signal)
    assert "WAIT" in text


# ---------------------------------------------------------------------------
# Списки и служебные экраны
# ---------------------------------------------------------------------------

def test_top_signals_list(long_signal):
    report = ScanReport(signals=[long_signal], scanned=42, universe_size=200)
    text = render.render_top_signals(report, limit=5)
    assert "Топ сигналы сейчас" in text
    assert "$TEST" in text
    assert "42" in text


def test_top_signals_empty_state():
    report = ScanReport(signals=[], scanned=10, universe_size=100)
    text = render.render_top_signals(report)
    assert "нет setups" in text


def test_scanner_table(long_signal):
    from app.domain.models import PrescreenCandidate, TickerInfo

    candidate = PrescreenCandidate(
        symbol="AAA/USDT", base="AAA", score=0.8,
        ticker=TickerInfo("AAA/USDT", last=1.0, quote_volume=1e7),
        reasons=["волатильность сжата"],
        metrics={"compression": 0.8, "change_7d": 2.0, "quote_volume": 1e7})
    report = ScanReport(candidates=[candidate], universe_size=250)
    text = render.render_scanner(report)
    assert "Сканер рынка" in text
    assert "$AAA" in text
    assert "сжата" in text


def test_menu_and_help_texts():
    menu = render.render_menu()
    for block in ("Найти перспективные монеты", "Глубокий анализ монеты",
                  "Топ сигналы сейчас", "Сканер рынка", "Настройки", "Помощь"):
        assert block in menu
    help_text = render.render_help()
    assert "Как читать сигнал" in help_text
    assert "Не является" in help_text


def test_settings_and_stats_render():
    from app.services.watchlist import UserSettings

    text = render.render_settings(UserSettings())
    assert "Настройки" in text
    assert "Риск-профиль" in text

    stats = render.render_stats(
        {"total": 5, "closed": 3, "open": 2, "win_rate": 66.7, "stop_rate": 33.3,
         "avg_r": 0.8, "total_r": 2.4,
         "calibration": [{"range": "6.5–8", "n": 2, "win_rate": 50.0, "avg_r": 0.5}]},
        {"universe": 200, "scanned": 35, "actionable": 3, "longs": 2, "shorts": 1,
         "avg_confidence": 6.8, "duration_s": 95.0},
        ["binance: здоров"],
    )
    assert "Статистика" in stats
    assert "calibration" not in stats
    assert "50%" in stats


def test_watchlist_render_empty_and_filled():
    assert "Список пуст" in render.render_watchlist([])
    text = render.render_watchlist(["BTC/USDT", "SOL/USDT"])
    assert "$BTC" in text and "$SOL" in text


def test_progress_render():
    text = render.render_progress("Сканирую", 0.4)
    assert "Сканирую" in text
    assert "40%" in text


def test_beginner_note_mentions_risk(long_signal):
    from app.signals.explain import beginner_note

    note = beginner_note(long_signal)
    assert "Стоп" in note
    assert "1–2%" in note
    assert "Не является" in note or "не является" in note
