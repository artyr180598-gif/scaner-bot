"""
tools/selftest.py — офлайн-проверка всего конвейера.

Гоняет ядро на синтетическом рынке (без сети и без Telegram) и печатает
человеческий отчёт: как работают скрининг, скоринг, план сделки и рендер.
Это быстрый способ убедиться, что рефакторинг ничего не сломал, когда биржи
недоступны (например, в CI или песочнице).

Запуск:
    PYTHONPATH=. .venv/bin/python -m tools.selftest
"""

from __future__ import annotations

import sys
from typing import Dict, List

from app.analysis.base import Group
from app.analysis.registry import all_modules
from app.config.settings import Settings
from app.data.synthetic import REGIMES, make_snapshot, make_universe
from app.domain.models import Direction, MarketContext
from app.presentation import render
from app.screening.prescreen import PrescreenConfig, coarse_screen, fine_screen
from app.services.scanner import ScannerService  # noqa: F401  (проверка импорта)
from app.signals.engine import SignalEngine

SEED = 7


def _ok(text: str) -> None:
    print(f"  ✅ {text}")


def _fail(text: str) -> None:
    print(f"  ❌ {text}")


def main() -> int:
    print("=" * 72)
    print("SELFTEST — офлайн-прогон ядра на синтетическом рынке")
    print("=" * 72)
    failures: List[str] = []

    modules = all_modules()
    print(f"\n1. Модули анализа: {len(modules)}")
    groups: Dict[str, int] = {}
    for m in modules:
        groups[m.group] = groups.get(m.group, 0) + 1
    for group, count in sorted(groups.items()):
        print(f"   {group:<14} {count}")
    if len(modules) < 20:
        failures.append("зарегистрировано подозрительно мало модулей")

    settings = Settings()
    settings.min_confidence = 4.0     # для демо показываем и слабые сигналы
    settings.min_rr = 1.1
    engine = SignalEngine(settings)
    context = MarketContext(btc_score=0.35, btc_trend="восходящий",
                            btc_direction=Direction.LONG,
                            breadth_24h_positive=0.62, median_change_24h=0.8)

    print("\n2. Анализ по режимам рынка (пороги публикации ослаблены для наглядности)")
    print("-" * 72)
    signals = {}
    for regime in REGIMES:
        snapshot = make_snapshot("TEST/USDT", regime, seed=SEED, bars=520)
        signal = engine.analyze(snapshot, context)
        signals[regime] = signal
        plan = signal.plan
        plan_text = "нет"
        if plan:
            plan_text = (f"вход {plan.entry_low:.5f}–{plan.entry_high:.5f}, "
                         f"стоп {plan.stop:.5f} ({plan.stop_pct:+.2f}%), "
                         f"цели {[round(t.price, 5) for t in plan.targets]}, "
                         f"R:R {plan.rr_primary:.2f}")
        print(f"  {regime:<13} → {signal.direction.value:<6} "
              f"уверенность {signal.confidence:4.1f}/10, счёт {signal.score:+.2f}, "
              f"факторов {len(signal.factors.factors)}")
        print(f"      сетап: {signal.setup}")
        print(f"      план:  {plan_text}")

    print("\n2b. Продуктовые пороги (настройки по умолчанию, риск-профиль moderate)")
    print("-" * 72)
    strict_engine = SignalEngine(Settings())
    strict_settings = Settings()
    print(f"  пороги: уверенность ≥ {strict_settings.min_confidence}, "
          f"R:R ≥ {strict_settings.min_rr}")
    strict_signals = {}
    for regime, seed in (("breakout", 21), ("downtrend", 7), ("accumulation", 7),
                         ("pumped", 7)):
        snap = make_snapshot("TEST/USDT", regime, seed=seed, bars=520)
        signal = strict_engine.analyze(snap, context)
        strict_signals[regime] = signal
        print(f"  {regime:<13} seed={seed:<3} → {signal.direction.value:<6} "
              f"уверенность {signal.confidence:4.1f}/10 · "
              f"{'СИГНАЛ' if signal.actionable else 'wait'}"
              + (f" · R:R {signal.plan.rr_primary:.2f}/{signal.plan.rr_avg:.2f}"
                 if signal.plan else ""))

    print("\n3. Проверка ожидаемого поведения")
    print("-" * 72)

    def check(condition: bool, text: str) -> None:
        (_ok if condition else _fail)(text)
        if not condition:
            failures.append(text)

    pumped = signals["pumped"]
    check(not pumped.actionable,
          "«pumped» (уже улетевшая монета) не публикуется как сигнал — анти-погоня")
    down = signals["downtrend"]
    check(down.score < 0.15,
          f"«downtrend» не даёт лонг (счёт {down.score:+.2f})")
    up = signals["breakout"]
    check(up.score > -0.15,
          f"«breakout» не даёт шорт (счёт {up.score:+.2f})")
    check(strict_signals["breakout"].actionable,
          "при продуктовых порогах сильный пробой публикуется как сигнал")
    check(not strict_signals["pumped"].actionable,
          "при продуктовых порогах улетевшая монета отсеивается")
    check(not strict_signals["accumulation"].actionable,
          "накопление без направления не публикуется (нет идеи по стороне)")
    for regime, signal in signals.items():
        if signal.plan:
            check(signal.plan.is_valid(), f"{regime}: план сделки валиден")
            check(len(signal.plan.targets) == 3, f"{regime}: три цели")
            check(signal.plan.rr_primary > 0.5, f"{regime}: R:R посчитан")
        check(0 <= signal.confidence <= 10, f"{regime}: уверенность в диапазоне 0..10")
        check(signal.data_confidence > 0.5, f"{regime}: качество данных высокое")
        check(bool(signal.summary), f"{regime}: есть объяснение")

    print("\n4. Скрининг: отбор «сжатой пружины» против улетевшей монеты")
    print("-" * 72)
    universe = make_universe(
        ["ACUM/USDT", "PUMP/USDT", "DOWN/USDT", "RNG/USDT", "BRK/USDT", "CAP/USDT"],
        ["accumulation", "pumped", "downtrend", "range", "breakout", "capitulation"],
        seed=SEED, bars=520)
    fine = fine_screen(universe, PrescreenConfig(fine_candidates=6),
                       signal_tf=None)
    ranked = [c.symbol for c in fine]
    print("   ранг тонкого скрининга:", " > ".join(ranked) or "пусто")
    check("ACUM/USDT" in ranked, "накапливающаяся монета прошла тонкий отбор")
    if ranked:
        check(ranked[0] != "PUMP/USDT",
              "уже улетевшая монета не на первом месте скрининга")

    tickers = {s.symbol: s.ticker for s in universe if s.ticker}
    coarse = coarse_screen(tickers, {"change_24h_median": 0.0, "change_24h_std": 2.5},
                           PrescreenConfig(min_quote_volume=1_000_000))
    print(f"   грубый отбор: {len(coarse)} кандидатов из {len(tickers)}")
    check(len(coarse) > 0, "грубый отбор работает на тикерах")

    print("\n5. Рендеринг интерфейса")
    print("-" * 72)
    demo = next((s for s in strict_signals.values() if s.actionable), None)
    if demo is None:
        demo = next((s for s in signals.values() if s.actionable), None)
    if demo is None:
        demo = next(iter(signals.values()))
    card = render.render_signal(demo, deposit=1000.0, context=context)
    deep = render.render_deep_analysis(demo, deposit=1000.0)
    help_text = render.render_help()
    menu = render.render_menu()
    for name, text in (("карточка сигнала", card), ("детали", deep),
                       ("помощь", help_text), ("меню", menu)):
        check(len(text) > 200, f"{name} отрендерилась ({len(text)} симв.)")
    check("<b>" in card, "карточка использует HTML-разметку")
    check("Не является" in card or demo.direction is Direction.WAIT,
          "в карточке есть дисклеймер")

    print("\n--- Пример карточки сигнала ---")
    print(card)
    print("\n--- Пример детального разбора (фрагмент) ---")
    print("\n".join(deep.splitlines()[:22]))

    print("\n" + "=" * 72)
    if failures:
        print(f"❌ SELFTEST: {len(failures)} проблем(ы):")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ SELFTEST: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
