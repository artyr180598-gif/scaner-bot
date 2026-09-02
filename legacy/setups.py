"""
setups.py — ядро проверенных торговых сетапов (v5).

Чем это отличается от «голосования индикаторов» (v4): здесь нет попытки
угадать направление по сумме мнений RSI/MACD/EMA. Есть закрытый список
паттернов, каждый из которых ПРОВЕРЕН на реальных исторических данных на
ДВУХ независимых наборах:

  * 290 фьючерсных монет Binance, 4h, 2019-09 → 2026-09 (+ реальные funding);
  * 63 спотовых монеты Binance, 1h, 2021-11 → 2026-05.

Методика и полные таблицы — `backtest/results/directional_v5.md`,
исследовательский код — `research/`.

Главный вывод исследования: подавляющее большинство «классических» правил
на крипте не работают после издержек (см. REJECTED_PATTERNS ниже). Работает
контрарианская логика на панике — и именно она здесь реализована.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

__all__ = [
    "SetupStats", "SetupMatch", "SetupContext", "SETUP_LIBRARY", "REJECTED_PATTERNS",
    "CALIBRATION", "calibration_note", "detect_setups", "describe_setup",
]


# ---------------------------------------------------------------------------
# Проверенная статистика (реальные цифры бэктеста, не оценки)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SetupStats:
    """Фактический результат сетапа на исторических данных."""

    trades: int
    win_rate: float          # %
    profit_factor: float
    avg_r: float
    train_pf: float          # до 2024-07 (период поиска правил)
    test_pf: float           # после 2024-07 (честный out-of-sample)
    worst_year: str
    sample: str

    def summary(self) -> str:
        return (f"на истории: {self.trades} сделок, win-rate {self.win_rate:.0f}%, "
                f"profit factor {self.profit_factor:.2f} "
                f"(вне обучающей выборки {self.test_pf:.2f})")


#: Цифры получены `research/portfolio.py`: стоп 4×ATR, цель 2R, до 5 позиций,
#: риск 1% на сделку, комиссия+проскальзывание 0.08% на ногу.
SETUP_STATS: dict[str, dict[str, SetupStats]] = {
    "panic_reversal": {
        "spot_1h": SetupStats(174, 59.2, 1.44, 0.151, 1.06, 2.19, "2021 (PF 0.57)",
                              "63 монеты, 1h, 2021-2026"),
        "perp_4h": SetupStats(442, 55.0, 1.48, 0.103, 1.12, 2.03, "2022 (PF 0.36)",
                              "290 монет, 4h, 2019-2026"),
    },
    "squeeze_breakdown": {
        "spot_1h": SetupStats(1040, 42.1, 1.13, 0.071, 1.00, 1.35, "2023 (PF 0.70)",
                              "63 монеты, 1h, 2021-2026"),
        "perp_4h": SetupStats(1039, 50.2, 1.19, 0.060, 1.30, 1.06, "2026 (PF 0.87)",
                              "290 монет, 4h, 2019-2026"),
    },
}

#: Калибровка: уверенность → что РЕАЛЬНО было на истории (обе панели вместе).
#: Именно этого не хватало в v4, где уверенность анти-коррелировала с итогом.
CALIBRATION: list[tuple[float, float, float, float, int]] = [
    # (нижняя граница, верхняя, фактический win-rate %, PF, число сделок)
    (0.0, 55.0, 51.4, 1.20, 37),
    (55.0, 60.0, 38.5, 0.91, 78),
    (60.0, 65.0, 51.0, 1.21, 157),
    (65.0, 70.0, 56.4, 1.40, 188),
    (70.0, 101.0, 58.8, 1.59, 240),
]


def calibration_note(confidence: float) -> str:
    """Что на самом деле было на истории у сигналов с такой уверенностью."""
    for lo, hi, win, pf, n in CALIBRATION:
        if lo <= confidence < hi:
            return (f"сигналы с уверенностью {lo:.0f}-{min(hi, 100):.0f}% на истории "
                    f"отработали в плюс в {win:.0f}% случаев (PF {pf:.2f}, {n} сделок)")
    return "статистики по такой уверенности нет"


#: Паттерны, которые ПРОВЕРЕНЫ и НЕ РАБОТАЮТ. Бот не выдаёт их как сигналы —
#: и честно говорит об этом, если пользователь их ждёт.
REJECTED_PATTERNS: list[tuple[str, str]] = [
    ("Перекупленность RSI>75 → шорт",
     "проверено на обеих панелях: PF 0.79 и 0.83, средний результат −0.12R. "
     "Перекупленность сама по себе не разворачивает рынок"),
    ("Пробой максимума с объёмом → лонг",
     "PF 0.90 (4h) и 0.73 (1h): после издержек пробои чаще ложные"),
    ("Тренд ADX>30 по стеку EMA",
     "PF 0.90-0.99, преимущества нет — следование тренду на 1h/4h съедается комиссией"),
    ("Funding перегрет → шорт",
     "PF 0.69-0.86: сам по себе высокий funding не разворачивает цену"),
    ("Фильтр «не входить в растянутое движение» (1.5·ATR от EMA20)",
     "A/B-тест v4: PF 1.014 против 1.025 без фильтра — гипотеза отклонена"),
    ("Сумма голосов индикаторов (ядро v4)",
     "10 345 сделок, PF 1.02; уверенность анти-коррелировала с итогом "
     "(корзина 75%+ дала PF 0.85). Заменено на проверенные сетапы"),
]


# ---------------------------------------------------------------------------
# Контекст: всё, что нужно сетапам, посчитанное по реальным свечам
# ---------------------------------------------------------------------------

@dataclass
class SetupContext:
    """Числа, по которым принимается решение. Любое поле может быть None."""

    base: str
    price: float
    atr: Optional[float] = None                 # ATR(14) базового ТФ
    atr_percent: Optional[float] = None         # ATR в % от цены
    change_24h: Optional[float] = None          # изменение цены за 24ч, доля (−0.2 = −20%)
    volume_z: Optional[float] = None            # z-score объёма последнего бара
    volume_ratio: Optional[float] = None        # объём / средний за 20
    market_change_24h: Optional[float] = None   # изменение BTC за 24ч, доля
    dollar_volume_24h: Optional[float] = None   # оборот в $ за сутки
    rsi: Optional[float] = None
    lower_wick: Optional[float] = None          # доля нижней тени в диапазоне бара
    upper_wick: Optional[float] = None
    bb_width_rank: Optional[float] = None       # перцентиль ширины полос (0..1)
    bb_position: Optional[float] = None         # 0 = нижняя полоса, 1 = верхняя
    dist_ema200_atr: Optional[float] = None     # (цена − EMA200) / ATR
    funding_rate: Optional[float] = None
    base_timeframe: str = "1h"
    bars: int = 0


@dataclass
class SetupMatch:
    """Сработавший сетап со всей конкретикой для карточки в Telegram."""

    key: str
    name: str
    side: str                      # long | short
    confidence: float              # 0..100, откалибровано по истории
    evidence: list[str] = field(default_factory=list)   # что совпало (простыми словами)
    warnings: list[str] = field(default_factory=list)
    stats: Optional[SetupStats] = None
    stats_other: Optional[SetupStats] = None
    sl_atr: float = 4.0
    tp_r: Sequence[float] = (1.0, 2.0, 3.0)
    tp_shares: Sequence[int] = (40, 40, 20)
    horizon_hours: int = 96
    horizon_text: str = "2-4 дня"
    idea: str = ""                 # одна фраза: в чём смысл сделки
    invalidation: str = ""
    tier: str = "проверенный"      # проверенный | экспериментальный

    @property
    def is_experimental(self) -> bool:
        return self.tier == "экспериментальный"


# ---------------------------------------------------------------------------
# Сетап A — «Паника рынка» (лонг). Основной, подтверждён на обеих панелях.
# ---------------------------------------------------------------------------

PANIC_MIN_DROP = 0.18          # монета должна упасть минимум на 18% за сутки
PANIC_MIN_VOL_Z = 0.5          # на повышенном объёме
PANIC_MARKET_DROP = 0.02       # и рынок (BTC) должен падать минимум на 2%
PANIC_MIN_TURNOVER = 200_000.0  # минимальный суточный оборот, $
PANIC_MIN_ATR_PCT = 0.5        # минимальная волатильность, % (иначе шум)
MIN_ACTIONABLE_CONFIDENCE = 60.0   # ниже — по истории преимущества нет


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _panic_confidence(ctx: SetupContext) -> float:
    """
    Формула из research/portfolio.py (conf_score). Проверено: win-rate растёт
    вместе с этим числом (60-65 → 51%, 65-70 → 56%, 70+ → 59%).
    """
    drop = -(ctx.change_24h or 0.0)
    depth = _clip01((drop - 0.15) / 0.20)
    vola = _clip01(((ctx.atr_percent or 0.0) / 100.0 - 0.01) / 0.04)
    turnover = ctx.dollar_volume_24h or 0.0
    liq = 0.0
    if turnover > 0:
        import math
        liq = _clip01((math.log10(max(turnover, 1e3)) - 5.0) / 2.0)
    mkt = _clip01((-(ctx.market_change_24h or 0.0) - 0.02) / 0.06)
    vol = _clip01(((ctx.volume_z or 0.0) - 0.5) / 2.5)
    raw = 0.32 * depth + 0.22 * vola + 0.20 * liq + 0.16 * mkt + 0.10 * vol
    return round(35.0 + 50.0 * raw, 1)


def _panic(ctx: SetupContext) -> Optional[SetupMatch]:
    if ctx.change_24h is None or ctx.market_change_24h is None or ctx.volume_z is None:
        return None
    if ctx.change_24h > -PANIC_MIN_DROP:
        return None
    if ctx.volume_z < PANIC_MIN_VOL_Z:
        return None
    if ctx.market_change_24h > -PANIC_MARKET_DROP:
        return None
    if (ctx.dollar_volume_24h or 0.0) < PANIC_MIN_TURNOVER:
        return None
    if (ctx.atr_percent or 0.0) < PANIC_MIN_ATR_PCT:
        return None

    conf = _panic_confidence(ctx)
    ev = [
        f"монета за сутки упала на {abs(ctx.change_24h) * 100:.1f}% — это паника, а не обычная коррекция",
        f"рынок падает вместе с ней: BTC {ctx.market_change_24h * 100:+.1f}% за сутки "
        f"(это ключевое условие — падение в одиночку обычно значит плохую новость и продолжается)",
        f"объём выше обычного (z-score {ctx.volume_z:+.1f}) — идёт массовый выход, "
        f"а такие распродажи выдыхаются быстрее всего",
    ]
    if (ctx.lower_wick or 0) > 0.3:
        ev.append(f"на последней свече длинная нижняя тень ({(ctx.lower_wick or 0) * 100:.0f}% диапазона) "
                  f"— снизу уже выкупают")
    if ctx.rsi is not None and ctx.rsi < 30:
        ev.append(f"RSI {ctx.rsi:.0f} — состояние глубокой перепроданности")
    if ctx.funding_rate is not None and ctx.funding_rate < 0:
        ev.append(f"funding отрицательный ({ctx.funding_rate * 100:.3f}%) — шортов набилось больше, "
                  f"чем лонгов, им платят за удержание; такие перекосы часто выносят вверх")

    warns = []
    if (ctx.dist_ema200_atr or 0) < -8:
        warns.append("монета уже очень далеко ниже своей долгосрочной средней — "
                     "это может быть не паника, а затяжной слив")
    if (ctx.dollar_volume_24h or 0) < 2_000_000:
        warns.append("оборот небольшой — реальная цена входа/выхода может заметно отличаться")

    return SetupMatch(
        key="panic_reversal",
        name="Разворот после паники",
        side="long",
        confidence=conf,
        evidence=ev,
        warnings=warns,
        stats=SETUP_STATS["panic_reversal"]["spot_1h"],
        stats_other=SETUP_STATS["panic_reversal"]["perp_4h"],
        sl_atr=4.0,
        tp_r=(1.0, 2.0, 3.0),
        tp_shares=(40, 40, 20),
        horizon_hours=96,
        horizon_text="2-4 дня",
        idea="покупаем страх: рынок валится, монету выбрасывают на повышенном объёме — "
             "такие распродажи чаще всего откупают в течение нескольких дней",
        invalidation="закрытие ниже стопа или новый обвал без рынка "
                     "(то есть когда BTC уже стабилизировался, а монета продолжает лететь вниз)",
        tier="проверенный",
    )


# ---------------------------------------------------------------------------
# Сетап B — «Слом сжатия вниз» (шорт). Слабее, помечен как экспериментальный.
# ---------------------------------------------------------------------------

SQUEEZE_MAX_WIDTH_RANK = 0.15
SQUEEZE_MAX_BB_POS = 0.05
SQUEEZE_MIN_VOL_RATIO = 1.3


def _squeeze(ctx: SetupContext) -> Optional[SetupMatch]:
    if ctx.bb_width_rank is None or ctx.bb_position is None or ctx.volume_ratio is None:
        return None
    if ctx.bb_width_rank > SQUEEZE_MAX_WIDTH_RANK:
        return None
    if ctx.bb_position > SQUEEZE_MAX_BB_POS:
        return None
    if ctx.volume_ratio < SQUEEZE_MIN_VOL_RATIO:
        return None
    if (ctx.dollar_volume_24h or 0.0) < PANIC_MIN_TURNOVER:
        return None
    if (ctx.atr_percent or 0.0) < PANIC_MIN_ATR_PCT:
        return None

    raw = (0.40 * _clip01((SQUEEZE_MAX_WIDTH_RANK - ctx.bb_width_rank) / SQUEEZE_MAX_WIDTH_RANK)
           + 0.30 * _clip01((ctx.volume_ratio - 1.3) / 1.5)
           + 0.30 * _clip01(-(ctx.dist_ema200_atr or 0.0) / 6.0))
    conf = round(35.0 + 50.0 * raw, 1)
    ev = [
        f"волатильность была сжата до минимума (полосы Боллинджера уже, чем "
        f"{(1 - ctx.bb_width_rank) * 100:.0f}% времени за последние месяцы) — рынок «сжал пружину»",
        "цена продавила нижнюю границу этого сжатия — пружина разжимается вниз",
        f"объём на сломе ×{ctx.volume_ratio:.1f} к среднему — движение подтверждено деньгами",
    ]
    warns = ["сетап слабее основного и нестабилен по периодам: "
             "на 4h он лучше работал до 2024 года, на 1h — после. Размер позиции стоит уменьшить"]
    return SetupMatch(
        key="squeeze_breakdown",
        name="Слом сжатия вниз",
        side="short",
        confidence=conf,
        evidence=ev,
        warnings=warns,
        stats=SETUP_STATS["squeeze_breakdown"]["perp_4h"],
        stats_other=SETUP_STATS["squeeze_breakdown"]["spot_1h"],
        sl_atr=4.0,
        tp_r=(1.0, 2.0, 3.0),
        tp_shares=(40, 40, 20),
        horizon_hours=72,
        horizon_text="1-3 дня",
        idea="после долгого затишья цена вываливается вниз на объёме — "
             "выход из сжатия обычно продолжается в ту же сторону",
        invalidation="возврат цены обратно внутрь полос Боллинджера "
                     "(значит слом был ложным)",
        tier="экспериментальный",
    )


SETUP_LIBRARY = (_panic, _squeeze)


def detect_setups(ctx: SetupContext) -> list[SetupMatch]:
    """Все сработавшие сетапы, самый уверенный — первым."""
    found = [m for m in (fn(ctx) for fn in SETUP_LIBRARY) if m is not None]
    found.sort(key=lambda m: (m.tier == "проверенный", m.confidence), reverse=True)
    return found


def near_misses(ctx: SetupContext) -> list[str]:
    """
    Почему НЕ вход: чего конкретно не хватило до срабатывания сетапов.
    Показываем только «близкие промахи», иначе список будет бесполезным.
    """
    out: list[str] = []
    ch = ctx.change_24h
    if ch is not None and ch < -0.08:
        parts = []
        if ch > -PANIC_MIN_DROP:
            parts.append(f"падение {abs(ch) * 100:.1f}% меньше нужных {PANIC_MIN_DROP * 100:.0f}%")
        if ctx.market_change_24h is not None and ctx.market_change_24h > -PANIC_MARKET_DROP:
            parts.append(f"рынок спокоен (BTC {ctx.market_change_24h * 100:+.1f}%), "
                         f"а падение в одиночку — обычно плохая новость по самой монете, "
                         f"такие покупать статистически убыточно")
        if ctx.volume_z is not None and ctx.volume_z < PANIC_MIN_VOL_Z:
            parts.append(f"нет всплеска объёма (z {ctx.volume_z:+.1f}) — паники не видно")
        if parts:
            out.append("сетап «разворот после паники» не сложился: " + "; ".join(parts))
    if ctx.bb_width_rank is not None and ctx.bb_width_rank < 0.25 and (ctx.bb_position or 1) > 0.05:
        out.append("волатильность сжата, но цена ещё не вышла из полос — "
                   "ждём, в какую сторону разожмётся пружина")
    return out


def describe_setup(key: str) -> str:
    """Описание сетапа для справки в боте."""
    texts = {
        "panic_reversal": (
            "🔻➡️🔺 <b>Разворот после паники</b> (лонг)\n"
            "Когда весь рынок валится и конкретную монету выбрасывают на повышенном "
            "объёме — это чаще всего вынужденные продажи (маржин-коллы, страх), "
            "а не переоценка стоимости. Такие распродажи откупают в течение нескольких дней.\n"
            "Обязательное условие: падает ВЕСЬ рынок. Если монета падает одна — "
            "это плохая новость именно по ней, и на истории такие входы убыточны "
            "(PF 0.71 против 1.77)."),
        "squeeze_breakdown": (
            "🎈💥 <b>Слом сжатия вниз</b> (шорт)\n"
            "Долгое затишье сжимает полосы Боллинджера. Когда цена вываливается "
            "вниз из такого сжатия на объёме, движение обычно продолжается.\n"
            "Сетап слабее основного, поэтому помечен как экспериментальный."),
    }
    return texts.get(key, "")
