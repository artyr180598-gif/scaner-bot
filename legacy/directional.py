"""
directional.py — направленное ядро анализа (Long/Short) v4.

ЗАЧЕМ ЭТОТ МОДУЛЬ
-----------------
`strategy.py` — рыночно-НЕЙТРАЛЬНОЕ ядро (спред спот↔перп, carry на funding).
Оно не отвечает на вопрос «куда пойдёт цена». Этот модуль отвечает: строит
направленный сигнал по реальным свечам с биржи и честно показывает, насколько
ему можно верить.

ПРИНЦИПЫ (нарушать нельзя)
--------------------------
1. Только реальные данные. Ни одно число не «додумывается»: если биржа не
   отдала таймфрейм/фандинг/стакан — фактор просто не участвует, а «уверенность
   анализа» падает.
2. Две РАЗНЫЕ уверенности:
     • Data Confidence  — качество и полнота данных (свечи, пропуски, свежесть,
       ликвидность, аномалии). Отвечает на вопрос «есть ли на чём считать».
     • Signal Confidence — согласованность факторов в одну сторону. Отвечает
       на вопрос «насколько дружно рынок говорит одно и то же».
   Signal Confidence НИКОГДА не выше Data Confidence и упирается в жёсткий
   потолок 92: 100% в рынке не бывает.
3. Всё объяснимо. Каждый фактор хранит свой вклад (score × вес) и человеческую
   формулировку — блок «почему» собирается из них, а не пишется отдельно.
4. Нет сигнала — это тоже ответ, с перечнем причин («почему НЕ вход»).

СТРУКТУРА
---------
    TimeframeFeatures  — посчитанные индикаторы одного ТФ
    DataQuality        — качество данных (0..100) + предупреждения
    Factor             — один фактор: score ∈ [-1..1], вес, объяснение
    TradePlan          — вход/стоп/тейки/RR/отмена/горизонт/размер позиции
    DirectionalSignal  — итог: направление, обе уверенности, план, тексты
    analyze()          — вход: MarketSnapshot → выход: DirectionalSignal
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import indicators as ind
from market_data import Candles, MarketSnapshot
from market_data import TIMEFRAME_SECONDS
from setups import (
    MIN_ACTIONABLE_CONFIDENCE, SetupContext, SetupMatch, calibration_note,
    detect_setups, near_misses,
)

__all__ = [
    "RiskProfile",
    "RISK_PROFILES",
    "DirectionalConfig",
    "TimeframeFeatures",
    "DataQuality",
    "Factor",
    "TradePlan",
    "DirectionalSignal",
    "analyze",
    "beginner_breakdown",
]


# ---------------------------------------------------------------------------
# Риск-профили
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskProfile:
    """
    Профиль риска пользователя: насколько строгие ворота допуска сигнала.

    Это НЕ «умножение прибыли», а именно фильтр: агрессивный профиль видит
    больше сигналов худшего качества, консервативный — меньше и лучше.
    """

    key: str
    title: str
    min_data_confidence: float
    min_signal_confidence: float
    min_rr: float
    risk_per_trade_percent: float
    min_quote_volume_24h: float
    #: потолок плеча для профиля
    max_leverage: float = 3.0
    #: какую долю маржи максимум может стоить срабатывание стопа (%)
    max_stop_loss_of_margin: float = 15.0
    allow_manipulation_flag: bool = False
    #: разрешать ли сетапы, помеченные как экспериментальные (слабое преимущество)
    allow_experimental: bool = False

    def describe(self) -> str:
        return (
            f"{self.title}: данные ≥{self.min_data_confidence:.0f}%, "
            f"сигнал ≥{self.min_signal_confidence:.0f}%, R/R ≥{self.min_rr:.1f}, "
            f"риск {self.risk_per_trade_percent:.1f}% депозита, "
            f"плечо до {self.max_leverage:g}×"
        )


RISK_PROFILES: dict[str, RiskProfile] = {
    "conservative": RiskProfile(
        key="conservative", title="🛡 Консервативный",
        min_data_confidence=75, min_signal_confidence=70, min_rr=2.0,
        risk_per_trade_percent=0.5, min_quote_volume_24h=50_000_000,
        max_leverage=3.0, max_stop_loss_of_margin=10.0,
    ),
    "moderate": RiskProfile(
        key="moderate", title="⚖️ Средний",
        min_data_confidence=65, min_signal_confidence=63, min_rr=1.6,
        risk_per_trade_percent=1.0, min_quote_volume_24h=10_000_000,
        max_leverage=5.0, max_stop_loss_of_margin=15.0,
    ),
    "aggressive": RiskProfile(
        key="aggressive", title="🔥 Агрессивный",
        min_data_confidence=55, min_signal_confidence=60, min_rr=1.3,
        risk_per_trade_percent=2.0, min_quote_volume_24h=2_000_000,
        max_leverage=10.0, max_stop_loss_of_margin=25.0,
        allow_manipulation_flag=True, allow_experimental=True,
    ),
}

DEFAULT_PROFILE = "moderate"


# ---------------------------------------------------------------------------
# Конфигурация анализа
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DirectionalConfig:
    """
    Параметры направленного движка.

    ВАЖНО про таймфреймы. Изначально дефолты были скальперские (вход 5m).
    Исследование (research/, отчёт backtest/results/directional_v5.md) это не
    подтвердило: на 5m проверить нечего — истории такой глубины в доступных
    данных нет, а издержки (0.16% на круг) на пятиминутных движениях съедают
    всё. Все сетапы проверены на 1h и 4h, поэтому вход — 1h, подтверждение —
    4h, контекст — 1d. 15m остаётся как справочный ТФ для картинки рынка.
    """

    entry_tf: str = "1h"
    confirm_tfs: tuple[str, ...] = ("4h",)
    context_tf: str = "1d"

    #: сколько закрытых баров минимально нужно на ТФ, чтобы он вообще считался
    min_bars: int = 60
    #: сколько баров считается «полными данными» (для Data Confidence)
    full_bars: int = 200

    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    bb_period: int = 20
    vol_ma_period: int = 20

    #: |net score| ≥ этого — есть направление
    min_abs_net: float = 0.15
    #: |net score|, при котором сила считается максимальной
    net_saturation: float = 0.55
    #: жёсткий потолок уверенности прогноза
    max_signal_confidence: float = 92.0
    #: потолок, если старший ТФ противоречит направлению
    conflict_confidence_cap: float = 60.0

    #: аномалия: движение бара больше N×ATR — флаг манипуляции/новостей
    anomaly_atr_multiple: float = 6.0
    #: аномалия: объём бара больше N× средний
    anomaly_volume_multiple: float = 25.0
    #: широкий спред стакана (%) — риск проскальзывания
    wide_book_percent: float = 0.35
    #: экстремальный funding (доля за интервал), выше — толпа перегрета
    extreme_funding: float = 0.0005          # 0.05% за 8ч ≈ 55% годовых
    #: «погоня за движением»: максимальное удаление цены от EMA20 входного ТФ
    #: В СТОРОНУ сигнала, в ATR. Больше — значит вход уже поздний и рискует
    #: попасть в откат. 0 = гейт выключен (ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ).
    #: ПРОВЕРЕНО на реальных данных (10 345 сделок, 2021-2026, 11 монет):
    #: гейт 1.5·ATR срезает ~15% сделок и НЕ улучшает результат
    #: (PF 1.014 против 1.025 без него) — поэтому выключен, но оставлен
    #: как настраиваемая опция. Само значение растяжения считается всегда
    #: и показывается пользователю.
    max_entry_stretch_atr: float = 0.0


DEFAULT_CONFIG = DirectionalConfig()

#: Веса ГРУПП факторов. Внутри группы факторы усредняются, поэтому «RSI на
#: трёх таймфреймах» не перевешивает «уровни», посчитанные один раз.
GROUP_WEIGHTS: dict[str, float] = {
    "trend": 2.2,
    "momentum": 1.6,
    "structure": 1.6,
    "levels": 1.5,
    "volume": 1.2,
    "divergence": 1.0,
    "volatility": 0.8,
    "derivatives": 0.8,
}

#: Потолок уверенности прогноза по числу НЕЗАВИСИМЫХ групп, согласных с итогом.
#: Это главная защита от «90% уверенности» на одном наблюдении, посчитанном
#: четырьмя разными способами.
GROUP_CONFIDENCE_CAPS: dict[int, float] = {0: 0.0, 1: 35.0, 2: 55.0, 3: 68.0, 4: 78.0, 5: 85.0}


# ---------------------------------------------------------------------------
# Фичи таймфрейма
# ---------------------------------------------------------------------------

@dataclass
class TimeframeFeatures:
    """Посчитанные по ЗАКРЫТЫМ барам индикаторы одного таймфрейма."""

    timeframe: str
    bars: int
    close: float
    ema_fast: Optional[float] = None
    ema_mid: Optional[float] = None
    ema_slow: Optional[float] = None
    ema_fast_slope: Optional[float] = None
    rsi: Optional[float] = None
    rsi_slope: Optional[float] = None
    macd_hist: Optional[float] = None
    macd_hist_prev: Optional[float] = None
    stoch_k: Optional[float] = None
    stoch_d: Optional[float] = None
    atr: Optional[float] = None
    atr_percent: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_width_percent: Optional[float] = None
    bb_width_rank: Optional[float] = None
    bb_position: Optional[float] = None       # 0 = нижняя полоса, 1 = верхняя
    volume_ratio: Optional[float] = None      # объём последнего бара / SMA20
    volume_trend: Optional[float] = None      # наклон объёма (норм.)
    obv_slope: Optional[float] = None
    resistances: list[float] = field(default_factory=list)
    supports: list[float] = field(default_factory=list)
    #: структура рынка: +1 = HH/HL (восходящая), −1 = LH/LL (нисходящая), 0 = боковик
    structure: int = 0
    structure_note: str = ""
    #: дивергенция цена/RSI: +1 бычья, −1 медвежья, 0 нет
    divergence: int = 0
    divergence_note: str = ""
    #: ближайший «круглый» уровень (магнит ликвидности)
    round_level: Optional[float] = None
    last_bar_ts: Optional[float] = None
    gaps: int = 0

    # ---- производные оценки ------------------------------------------------
    @property
    def trend_score(self) -> Optional[float]:
        """
        Оценка тренда/структуры по ТФ ∈ [-1..1] из положения цены и стека EMA.
        Каждый компонент — простое «да/нет», сумма нормируется по доступным.
        """
        parts: list[float] = []
        if self.ema_fast is not None:
            parts.append(1.0 if self.close > self.ema_fast else -1.0)
        if self.ema_mid is not None:
            parts.append(1.0 if self.close > self.ema_mid else -1.0)
        if self.ema_slow is not None:
            parts.append(1.0 if self.close > self.ema_slow else -1.0)
        if self.ema_fast is not None and self.ema_mid is not None:
            parts.append(1.0 if self.ema_fast > self.ema_mid else -1.0)
        if self.ema_mid is not None and self.ema_slow is not None:
            parts.append(1.0 if self.ema_mid > self.ema_slow else -1.0)
        if self.ema_fast_slope is not None:
            parts.append(_clamp(self.ema_fast_slope * 200.0, -1.0, 1.0))
        if not parts:
            return None
        return _clamp(sum(parts) / len(parts), -1.0, 1.0)


def build_features(
    candles: Candles, cfg: DirectionalConfig, now: float
) -> Optional[TimeframeFeatures]:
    """
    Считает индикаторы по ЗАКРЫТЫМ барам таймфрейма.

    Последний бар отбрасывается, если он ещё формируется (его время открытия
    + длительность ТФ > сейчас): решения на незакрытом баре — классическая
    причина «сигнал был, а потом исчез».
    """
    step = candles.step_seconds or TIMEFRAME_SECONDS.get(candles.timeframe, 0)
    series = candles
    if step and candles.last_ts is not None and candles.last_ts + step > now + 1:
        series = candles.dropped_last()
    n = len(series)
    if n < cfg.min_bars:
        return None

    closes = list(series.close)
    highs = list(series.high)
    lows = list(series.low)
    vols = list(series.volume)

    f = TimeframeFeatures(
        timeframe=series.timeframe,
        bars=n,
        close=closes[-1],
        last_bar_ts=series.ts[-1],
    )

    ema_f = ind.ema(closes, cfg.ema_fast)
    ema_m = ind.ema(closes, cfg.ema_mid)
    ema_s = ind.ema(closes, cfg.ema_slow)
    f.ema_fast = ema_f[-1]
    f.ema_mid = ema_m[-1]
    f.ema_slow = ema_s[-1]
    tail_fast = [v for v in ema_f[-6:] if v is not None]
    if len(tail_fast) >= 3 and f.close:
        slope = ind.linreg_slope(tail_fast)
        f.ema_fast_slope = (slope / f.close) if slope is not None else None

    rsi_series = ind.rsi(closes, cfg.rsi_period)
    f.rsi = rsi_series[-1]
    rsi_tail = [v for v in rsi_series[-6:] if v is not None]
    if len(rsi_tail) >= 3:
        f.rsi_slope = ind.linreg_slope(rsi_tail)

    _, _, hist = ind.macd(closes)
    f.macd_hist = hist[-1]
    f.macd_hist_prev = hist[-2] if len(hist) > 1 else None

    k, d = ind.stochastic(highs, lows, closes)
    f.stoch_k, f.stoch_d = k[-1], d[-1]

    atr_series = ind.atr(highs, lows, closes, cfg.atr_period)
    f.atr = atr_series[-1]
    if f.atr and f.close:
        f.atr_percent = f.atr / f.close * 100.0

    up, mid, low = ind.bollinger(closes, cfg.bb_period)
    f.bb_upper, f.bb_lower = up[-1], low[-1]
    if up[-1] is not None and low[-1] is not None and mid[-1]:
        width = (up[-1] - low[-1]) / mid[-1] * 100.0
        f.bb_width_percent = width
        history = [
            (u - l) / m * 100.0
            for u, m, l in zip(up, mid, low)
            if u is not None and l is not None and m
        ]
        f.bb_width_rank = ind.percentile_rank(history[-120:], width)
        span = up[-1] - low[-1]
        f.bb_position = _clamp((closes[-1] - low[-1]) / span, -0.5, 1.5) if span > 0 else 0.5

    vol_ma = ind.sma(vols, cfg.vol_ma_period)
    if vol_ma[-1]:
        f.volume_ratio = vols[-1] / float(vol_ma[-1])
        recent = [v for v in vol_ma[-10:] if v is not None]
        if len(recent) >= 3:
            slope = ind.linreg_slope(recent)
            f.volume_trend = (slope / float(vol_ma[-1])) if slope else 0.0
    obv_series = ind.obv(closes, vols)
    obv_tail = obv_series[-20:]
    scale = max(abs(v) for v in obv_tail) or 1.0
    slope = ind.linreg_slope([v / scale for v in obv_tail])
    f.obv_slope = slope

    # Уровни: фракталы последних баров, схлопнутые в кластеры по 0.35·ATR
    tol = (f.atr or (f.close * 0.002)) * 0.35
    hi_idx = ind.swing_highs(highs, 2, 2)
    lo_idx = ind.swing_lows(lows, 2, 2)
    f.resistances = _cluster([highs[i] for i in hi_idx if highs[i] > f.close], tol)
    f.supports = _cluster([lows[i] for i in lo_idx if lows[i] < f.close], tol, reverse=True)

    # Структура рынка: сравниваем два последних значимых хая и лоя.
    # Higher High + Higher Low = восходящая структура (и наоборот).
    hi_vals = [highs[i] for i in hi_idx][-3:]
    lo_vals = [lows[i] for i in lo_idx][-3:]
    if len(hi_vals) >= 2 and len(lo_vals) >= 2:
        hh = hi_vals[-1] > hi_vals[-2]
        hl = lo_vals[-1] > lo_vals[-2]
        if hh and hl:
            f.structure, f.structure_note = 1, "растущие максимумы и минимумы (HH/HL)"
        elif not hh and not hl:
            f.structure, f.structure_note = -1, "падающие максимумы и минимумы (LH/LL)"
        else:
            f.structure, f.structure_note = 0, "максимумы и минимумы вразнобой — боковик"

    # Дивергенция цена/RSI на последних 40 барах: цена сделала новый экстремум,
    # а RSI — нет. Классический признак истощения движения.
    look = 40
    if len(closes) >= look and rsi_series[-1] is not None:
        seg_close = closes[-look:]
        seg_rsi = rsi_series[-look:]
        pairs = [(i, v) for i, v in enumerate(seg_rsi) if v is not None]
        if len(pairs) >= look // 2:
            half = len(seg_close) // 2
            lo1 = min(range(half), key=lambda i: seg_close[i])
            lo2 = half + min(range(len(seg_close) - half), key=lambda i: seg_close[half + i])
            hi1 = max(range(half), key=lambda i: seg_close[i])
            hi2 = half + max(range(len(seg_close) - half), key=lambda i: seg_close[half + i])
            r = {i: seg_rsi[i] for i in (lo1, lo2, hi1, hi2) if seg_rsi[i] is not None}
            if lo1 in r and lo2 in r and seg_close[lo2] < seg_close[lo1] and r[lo2] > r[lo1]:
                f.divergence = 1
                f.divergence_note = (
                    f"бычья дивергенция: цена сделала более низкий минимум, "
                    f"а RSI — более высокий ({r[lo1]:.0f} → {r[lo2]:.0f})"
                )
            elif hi1 in r and hi2 in r and seg_close[hi2] > seg_close[hi1] and r[hi2] < r[hi1]:
                f.divergence = -1
                f.divergence_note = (
                    f"медвежья дивергенция: цена сделала более высокий максимум, "
                    f"а RSI — более низкий ({r[hi1]:.0f} → {r[hi2]:.0f})"
                )

    # Ближайший круглый уровень — магнит ликвидности (стопы/лимитки толпы)
    f.round_level = _nearest_round(f.close)

    # Пропуски баров (биржа отдала не всё) — важный сигнал качества данных
    if step:
        expected = [series.ts[0] + i * step for i in range(n)]
        f.gaps = sum(1 for a, b in zip(series.ts, expected) if abs(a - b) > step * 0.5)
    return f


def _nearest_round(price: float) -> Optional[float]:
    """
    Ближайший «круглый» уровень цены — там традиционно стоят лимитки и стопы
    толпы, и цена к ним липнет.

    Берём самый КРУПНЫЙ шаг сетки (1/2.5/5/10 × 10^k), у которого ближайший
    уровень всё ещё рядом с ценой (в пределах 1%). Для 77 448 это даст 77 000,
    а не бессмысленные 75 000.
    """
    if price <= 0:
        return None
    base = 10 ** math.floor(math.log10(price) - 2)
    best: Optional[float] = None
    for mult in (1.0, 2.5, 5.0, 10.0, 25.0, 50.0):
        step = base * mult
        if step <= 0:
            continue
        below = math.floor(price / step) * step
        above = below + step
        level = below if (price - below) <= (above - price) else above
        if abs(level - price) <= price * 0.01:
            best = level      # шаги перебираются по возрастанию — берём крупнейший
    return best


def _cluster(levels: Sequence[float], tolerance: float, reverse: bool = False) -> list[float]:
    """Схлопывает близкие уровни в один (среднее кластера)."""
    if not levels:
        return []
    ordered = sorted(levels, reverse=reverse)
    out: list[list[float]] = [[ordered[0]]]
    for lvl in ordered[1:]:
        if abs(lvl - out[-1][-1]) <= tolerance:
            out[-1].append(lvl)
        else:
            out.append([lvl])
    return [sum(group) / len(group) for group in out]


# ---------------------------------------------------------------------------
# Качество данных
# ---------------------------------------------------------------------------

@dataclass
class DataQuality:
    """Насколько данным можно верить: 0..100 + человеческие пояснения."""

    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    manipulation_flag: bool = False
    liquidity_usd: Optional[float] = None
    stale_seconds: Optional[float] = None

    def describe(self) -> list[str]:
        rows = [f"{k}: {v:.0f}/100" for k, v in self.components.items()]
        return rows


def assess_data(
    snap: MarketSnapshot,
    feats: dict[str, TimeframeFeatures],
    cfg: DirectionalConfig,
    profile: RiskProfile,
    now: float,
) -> DataQuality:
    """
    Считает Data Confidence из 5 независимых компонент (каждая 0..100):

      coverage   — сколько нужных ТФ реально получено
      history    — длина истории (баров) относительно `full_bars`
      integrity  — пропуски баров / нулевые объёмы
      freshness  — насколько свежий последний бар относительно ТФ
      liquidity  — 24ч оборот и ширина стакана

    Итог — среднее с весами; аномалии (пампы/дампы, залипший рынок) режут
    результат отдельным штрафом и поднимают флаг манипуляции.
    """
    dq = DataQuality()
    needed = [cfg.entry_tf, *cfg.confirm_tfs, cfg.context_tf]
    have = [tf for tf in needed if tf in feats]

    # 1. Покрытие таймфреймов
    dq.components["покрытие ТФ"] = len(have) / len(needed) * 100.0
    for tf in needed:
        if tf not in feats:
            reason = "нет данных" if tf not in snap.candles else (
                f"мало баров ({len(snap.candles[tf])} < {cfg.min_bars})"
            )
            dq.warnings.append(f"таймфрейм {tf}: {reason}")

    if cfg.entry_tf not in feats:
        dq.blockers.append(
            f"нет рабочего таймфрейма {cfg.entry_tf} — считать сигнал не на чем"
        )

    # 2. Длина истории
    if have:
        ratios = [min(1.0, feats[tf].bars / cfg.full_bars) for tf in have]
        dq.components["история"] = sum(ratios) / len(ratios) * 100.0
    else:
        dq.components["история"] = 0.0

    # 3. Целостность (пропуски + пустые бары)
    if have:
        integrity_parts = []
        for tf in have:
            f = feats[tf]
            gap_ratio = f.gaps / max(1, f.bars)
            candles = snap.candles[tf]
            zero_vol = sum(1 for v in candles.volume if v <= 0) / max(1, len(candles))
            integrity_parts.append(max(0.0, 1.0 - gap_ratio * 2.0 - zero_vol * 1.5))
            if gap_ratio > 0.02:
                dq.warnings.append(
                    f"{tf}: пропуски в свечах {gap_ratio * 100:.1f}% — биржа отдала не всё"
                )
            if zero_vol > 0.05:
                dq.warnings.append(
                    f"{tf}: {zero_vol * 100:.0f}% баров с нулевым объёмом — рынок «залипает»"
                )
        dq.components["целостность"] = sum(integrity_parts) / len(integrity_parts) * 100.0
    else:
        dq.components["целостность"] = 0.0

    # 4. Свежесть
    entry = feats.get(cfg.entry_tf)
    if entry and entry.last_bar_ts:
        step = TIMEFRAME_SECONDS.get(cfg.entry_tf, 300)
        age = now - (entry.last_bar_ts + step)
        dq.stale_seconds = max(0.0, age)
        fresh_ratio = _clamp(1.0 - max(0.0, age) / (step * 3.0), 0.0, 1.0)
        dq.components["свежесть"] = fresh_ratio * 100.0
        if age > step * 3:
            dq.warnings.append(
                f"последняя свеча {cfg.entry_tf} старше {age / 60:.0f} мин — данные несвежие"
            )
    else:
        dq.components["свежесть"] = 0.0

    # 5. Ликвидность
    liq_score = 50.0
    vol24 = snap.quote_volume_24h
    dq.liquidity_usd = vol24
    if vol24 is None:
        dq.warnings.append("нет данных по 24ч обороту — ликвидность не проверена")
        liq_score = 40.0
    else:
        # 100 баллов при обороте ≥ 10× минимума профиля, 0 — при нулевом
        ref = max(profile.min_quote_volume_24h, 1.0)
        liq_score = _clamp(math.log10(max(vol24, 1.0) / ref) / 1.0 * 50.0 + 50.0, 0.0, 100.0)
        if vol24 < profile.min_quote_volume_24h:
            dq.warnings.append(
                f"оборот ${vol24 / 1e6:.2f}M за 24ч ниже порога профиля "
                f"(${profile.min_quote_volume_24h / 1e6:.0f}M) — риск проскальзывания и манипуляций"
            )
    if snap.book is not None:
        spread = snap.book.spread_percent
        if spread > cfg.wide_book_percent:
            liq_score = min(liq_score, 45.0)
            dq.warnings.append(
                f"широкий стакан: спред {spread:.2f}% — вход/выход будет дорогим"
            )
        depth = snap.book.bid_volume_usd + snap.book.ask_volume_usd
        if depth and depth < 20_000:
            liq_score = min(liq_score, 40.0)
            dq.warnings.append(
                f"тонкий стакан: ~${depth:,.0f} в 20 уровнях — легко двинуть ценой"
            )
    dq.components["ликвидность"] = liq_score

    # Аномалии: экстремальный бар / объёмный выброс
    if entry and entry.atr:
        candles = snap.candles[cfg.entry_tf]
        rng = candles.high[-1] - candles.low[-1]
        if rng > entry.atr * cfg.anomaly_atr_multiple:
            dq.manipulation_flag = True
            dq.warnings.append(
                f"аномальная свеча: диапазон {rng / entry.atr:.1f}×ATR — "
                f"похоже на памп/дамп или новостной вынос"
            )
    if entry and entry.volume_ratio and entry.volume_ratio > cfg.anomaly_volume_multiple:
        dq.manipulation_flag = True
        dq.warnings.append(
            f"объём последнего бара ×{entry.volume_ratio:.0f} к среднему — "
            f"возможна манипуляция/иксовый вынос"
        )

    # Молодой листинг: истории мало, индикаторы врут, волатильность экстремальна
    if snap.listing_age_days is not None and snap.listing_age_days < 30:
        dq.warnings.append(
            f"монета торгуется всего ~{snap.listing_age_days:.0f} дней — "
            f"истории мало, поведение непредсказуемо"
        )
        dq.components["история"] = min(dq.components.get("история", 100.0), 45.0)
    if snap.btc_correlation is not None and abs(snap.btc_correlation) > 0.85:
        dq.warnings.append(
            f"корреляция с BTC {snap.btc_correlation:+.2f} — монета почти повторяет "
            f"биткоин: реальный риск здесь = риск BTC"
        )

    for err in snap.errors:
        dq.warnings.append(f"источник данных: {err}")

    weights = {
        "покрытие ТФ": 1.2,
        "история": 1.0,
        "целостность": 1.3,
        "свежесть": 1.0,
        "ликвидность": 1.5,
    }
    total_w = sum(weights[k] for k in dq.components)
    dq.score = sum(dq.components[k] * weights[k] for k in dq.components) / total_w
    if dq.manipulation_flag:
        dq.score *= 0.75
    if snap.errors:
        dq.score *= max(0.7, 1.0 - 0.05 * len(snap.errors))
    if dq.blockers:
        dq.score = min(dq.score, 25.0)
    dq.score = _clamp(dq.score, 0.0, 100.0)
    return dq


# ---------------------------------------------------------------------------
# Факторы
# ---------------------------------------------------------------------------

@dataclass
class Factor:
    """
    Один фактор анализа.

    score  ∈ [-1..1]: −1 = максимально «вниз», +1 = максимально «вверх».
    weight — вклад в итог. Фактор без данных просто не создаётся.
    note   — человеческое объяснение, из него собирается блок «почему».
    """

    key: str
    title: str
    score: float
    weight: float
    note: str
    #: группа независимости: факторы одной группы (тренд на 4 ТФ, RSI на 3 ТФ)
    #: сильно коррелированы и НЕ должны считаться отдельными «голосами»
    category: str = "other"

    @property
    def contribution(self) -> float:
        return self.score * self.weight

    def arrow(self) -> str:
        if self.score > 0.25:
            return "🟢"
        if self.score < -0.25:
            return "🔴"
        return "⚪️"


def build_factors(
    snap: MarketSnapshot,
    feats: dict[str, TimeframeFeatures],
    cfg: DirectionalConfig,
    carry_note: Optional[str] = None,
) -> list[Factor]:
    """Собирает все доступные факторы. Нет данных для фактора → фактора нет."""
    factors: list[Factor] = []
    entry = feats.get(cfg.entry_tf)

    # --- 1. Тренд по каждому ТФ ---------------------------------------------
    tf_weights = {cfg.entry_tf: 1.0}
    for i, tf in enumerate(cfg.confirm_tfs):
        tf_weights[tf] = 1.5 + 0.2 * i
    tf_weights[cfg.context_tf] = 1.4
    for tf, weight in tf_weights.items():
        f = feats.get(tf)
        if f is None:
            continue
        score = f.trend_score
        if score is None:
            continue
        pos = "выше" if score > 0 else "ниже"
        factors.append(Factor(
            key=f"trend_{tf}",
            title=f"Тренд {tf}",
            category="trend",
            score=score,
            weight=weight,
            note=(
                f"на {tf} цена {pos} своих скользящих "
                f"(EMA{cfg.ema_fast}/{cfg.ema_mid}"
                f"{'/' + str(cfg.ema_slow) if f.ema_slow else ''}), "
                f"структура {'растущая' if score > 0 else 'падающая'}"
            ),
        ))

    # --- 2. Мультитаймфреймовое согласование --------------------------------
    tf_scores = {
        tf: feats[tf].trend_score for tf in tf_weights
        if tf in feats and feats[tf].trend_score is not None
    }
    if len(tf_scores) >= 2:
        signs = [1 if s > 0.1 else (-1 if s < -0.1 else 0) for s in tf_scores.values()]  # type: ignore[operator]
        non_zero = [s for s in signs if s != 0]
        if non_zero:
            align = sum(non_zero) / len(non_zero)     # −1..1
            agree_pct = abs(align) * 100.0
            direction = "вверх" if align > 0 else "вниз"
            factors.append(Factor(
                key="mtf",
                title="Согласование ТФ",
                category="trend",
                score=align * (len(non_zero) / len(signs)),
                weight=2.0,
                note=(
                    f"{len(non_zero)} из {len(signs)} таймфреймов смотрят в одну сторону "
                    f"({agree_pct:.0f}% согласия, {direction})"
                    if agree_pct > 60 else
                    f"таймфреймы спорят между собой ({agree_pct:.0f}% согласия) — "
                    f"рынок без единого тренда"
                ),
            ))

    # --- 3. Моментум: RSI ----------------------------------------------------
    for tf, weight in ((cfg.entry_tf, 0.8), *((t, 1.0) for t in cfg.confirm_tfs)):
        f = feats.get(tf)
        if f is None or f.rsi is None:
            continue
        rsi_v = f.rsi
        # центрируем: 50 = нейтраль; экстремумы трактуем как истощение
        raw = (rsi_v - 50.0) / 25.0
        if rsi_v > 75:
            raw = 0.35            # перегрев: слабый плюс, а не «сильно вверх»
        elif rsi_v < 25:
            raw = -0.35
        if f.rsi_slope is not None:
            raw += _clamp(f.rsi_slope / 5.0, -0.4, 0.4)
        state = (
            "перекуплен" if rsi_v > 70 else
            "перепродан" if rsi_v < 30 else
            "в норме"
        )
        factors.append(Factor(
            key=f"rsi_{tf}",
            title=f"RSI {tf}",
            category="momentum",
            score=_clamp(raw, -1.0, 1.0),
            weight=weight,
            note=f"RSI({cfg.rsi_period}) на {tf} = {rsi_v:.0f} ({state})",
        ))

    # --- 4. MACD -------------------------------------------------------------
    for tf, weight in ((cfg.entry_tf, 0.9), *((t, 1.1) for t in cfg.confirm_tfs)):
        f = feats.get(tf)
        if f is None or f.macd_hist is None or not f.close:
            continue
        norm = f.macd_hist / f.close * 100.0        # % от цены
        score = _clamp(norm * 4.0, -1.0, 1.0)
        growing = (
            f.macd_hist_prev is not None and abs(f.macd_hist) > abs(f.macd_hist_prev)
        )
        factors.append(Factor(
            key=f"macd_{tf}",
            title=f"MACD {tf}",
            category="momentum",
            score=score,
            weight=weight,
            note=(
                f"MACD-гистограмма на {tf} {'положительная' if norm > 0 else 'отрицательная'}"
                f" ({norm:+.3f}% от цены), импульс "
                f"{'усиливается' if growing else 'затухает'}"
            ),
        ))

    # --- 5. Стохастик --------------------------------------------------------
    if entry and entry.stoch_k is not None and entry.stoch_d is not None:
        k, d = entry.stoch_k, entry.stoch_d
        score = _clamp((k - 50.0) / 40.0, -1.0, 1.0) * 0.6 + (0.4 if k > d else -0.4)
        factors.append(Factor(
            key="stoch",
            title="Стохастик",
            category="momentum",
            score=_clamp(score, -1.0, 1.0),
            weight=0.6,
            note=(
                f"Стохастик {cfg.entry_tf}: %K={k:.0f}, %D={d:.0f} — "
                f"{'быстрый выше медленного (давление вверх)' if k > d else 'быстрый ниже медленного (давление вниз)'}"
            ),
        ))

    # --- 6. Объём ------------------------------------------------------------
    if entry and entry.volume_ratio is not None:
        vr = entry.volume_ratio
        obv = entry.obv_slope or 0.0
        direction_score = _clamp(obv * 8.0, -1.0, 1.0)
        strength = _clamp((vr - 1.0), -0.5, 1.5)
        score = _clamp(direction_score * (0.5 + min(strength, 1.0) * 0.5), -1.0, 1.0)
        factors.append(Factor(
            key="volume",
            title="Объём",
            category="volume",
            score=score,
            weight=1.2,
            note=(
                f"объём последнего бара ×{vr:.2f} к среднему за {cfg.vol_ma_period}; "
                f"накопленный объём (OBV) {'растёт — покупатель активнее' if obv > 0 else 'падает — продавец активнее' if obv < 0 else 'стоит на месте'}"
            ),
        ))

    # --- 7. Уровни поддержки/сопротивления ----------------------------------
    level_tf = cfg.confirm_tfs[0] if cfg.confirm_tfs and cfg.confirm_tfs[0] in feats else cfg.entry_tf
    lf = feats.get(level_tf)
    if lf and lf.atr:
        price = lf.close
        sup = lf.supports[0] if lf.supports else None
        res = lf.resistances[0] if lf.resistances else None
        if sup or res:
            d_sup = (price - sup) / lf.atr if sup else None
            d_res = (res - price) / lf.atr if res else None
            score = 0.0
            if d_sup is not None and d_res is not None:
                # ближе к поддержке → плюс (место для лонга), к сопротивлению → минус
                total = d_sup + d_res
                score = _clamp((d_res - d_sup) / total, -1.0, 1.0) if total > 0 else 0.0
            elif d_sup is not None:
                score = _clamp(1.0 - d_sup / 3.0, -1.0, 1.0)
            elif d_res is not None:
                score = _clamp(d_res / 3.0 - 1.0, -1.0, 1.0)
            bits = []
            if sup:
                bits.append(f"поддержка {_fmt(sup)} ({d_sup:.1f} ATR ниже)")
            if res:
                bits.append(f"сопротивление {_fmt(res)} ({d_res:.1f} ATR выше)")
            factors.append(Factor(
                key="levels",
                title="Уровни",
                category="levels",
                score=score,
                weight=1.5,
                note=f"на {level_tf}: " + ", ".join(bits),
            ))

    # --- 8. Волатильность / Боллинджер ---------------------------------------
    if entry and entry.bb_position is not None:
        pos = entry.bb_position
        # у нижней полосы в растущем рынке — интересно на лонг, и наоборот
        score = _clamp((0.5 - pos) * 1.2, -1.0, 1.0)
        squeeze = (
            entry.bb_width_rank is not None and entry.bb_width_rank < 20
        )
        factors.append(Factor(
            key="bbands",
            title="Боллинджер",
            category="volatility",
            score=score,
            weight=0.7,
            note=(
                f"цена в полосах Боллинджера на {pos * 100:.0f}% высоты "
                f"({'у нижней границы — растянуто вниз' if pos < 0.25 else 'у верхней границы — растянуто вверх' if pos > 0.75 else 'в середине канала'})"
                + (
                    f"; ширина канала в нижних {entry.bb_width_rank:.0f}% истории — "
                    f"сжатие, часто предшествует резкому движению" if squeeze else ""
                )
            ),
        ))

    # --- 8b. Структура рынка (HH/HL vs LH/LL) --------------------------------
    for tf, weight in ((cfg.entry_tf, 0.8), *((t, 1.3) for t in cfg.confirm_tfs)):
        f = feats.get(tf)
        if f is None or not f.structure_note:
            continue
        factors.append(Factor(
            key=f"structure_{tf}",
            title=f"Структура {tf}",
            category="structure",
            score=float(f.structure) * 0.9,
            weight=weight,
            note=f"на {tf} {f.structure_note}",
        ))

    # --- 8c. Дивергенции цена/RSI --------------------------------------------
    for tf, weight in ((cfg.entry_tf, 0.7), *((t, 1.0) for t in cfg.confirm_tfs)):
        f = feats.get(tf)
        if f is None or not f.divergence:
            continue
        factors.append(Factor(
            key=f"divergence_{tf}",
            title=f"Дивергенция {tf}",
            category="divergence",
            score=float(f.divergence) * 0.8,
            weight=weight,
            note=f"на {tf} {f.divergence_note} — импульс выдыхается",
        ))

    # --- 9. Фандинг (перп) ---------------------------------------------------
    if snap.funding_rate is not None:
        fr = snap.funding_rate
        annual = fr * (24.0 / max(snap.funding_interval_hours, 1e-9)) * 365.0 * 100.0
        # экстремальный фандинг = толпа в одну сторону = контр-сигнал
        score = _clamp(-fr / (cfg.extreme_funding * 2.0), -1.0, 1.0)
        side = "лонги платят шортам" if fr > 0 else "шорты платят лонгам"
        factors.append(Factor(
            key="funding",
            title="Funding",
            category="derivatives",
            score=score,
            weight=0.8 if abs(fr) > cfg.extreme_funding else 0.4,
            note=(
                f"ставка финансирования {fr * 100:+.4f}% за {snap.funding_interval_hours:.0f}ч "
                f"(~{annual:+.0f}% годовых), {side}"
                + ("; перекос толпы — риск выноса против неё" if abs(fr) > cfg.extreme_funding else "")
            ),
        ))

    # --- 10. Открытый интерес ------------------------------------------------
    if snap.open_interest_change_percent is not None and entry:
        oi_chg = snap.open_interest_change_percent
        price_chg = 0.0
        candles = snap.candles.get(cfg.entry_tf)
        if candles and len(candles) > 12:
            price_chg = (candles.close[-1] / candles.close[-12] - 1.0) * 100.0
        agree = 1.0 if (oi_chg > 0 and price_chg > 0) else (-1.0 if (oi_chg > 0 and price_chg < 0) else 0.3 * _sign(price_chg))
        factors.append(Factor(
            key="oi",
            title="Открытый интерес",
            category="derivatives",
            score=_clamp(agree * min(1.0, abs(oi_chg) / 5.0), -1.0, 1.0),
            weight=0.5,
            note=(
                f"открытый интерес {oi_chg:+.1f}% при движении цены {price_chg:+.2f}% — "
                f"{'в позицию заходят по тренду' if oi_chg > 0 and price_chg != 0 else 'позиции закрывают'}"
            ),
        ))

    # --- 11. Соотношение лонг/шорт (если биржа отдаёт) -----------------------
    if snap.long_short_ratio is not None and snap.long_short_ratio > 0:
        ls = snap.long_short_ratio
        skew = math.log(ls)                      # >0 — перевес лонгов
        factors.append(Factor(
            key="long_short",
            title="Лонг/шорт",
            category="derivatives",
            score=_clamp(-skew * 1.5, -1.0, 1.0),   # толпа в одну сторону = риск выноса
            weight=0.5,
            note=(
                f"соотношение лонгов к шортам {ls:.2f} — "
                f"{'толпа в лонгах, рынок любит выносить большинство' if ls > 1.3 else 'толпа в шортах, возможен вынос вверх' if ls < 0.77 else 'позиции сбалансированы'}"
            ),
        ))

    return factors


# ---------------------------------------------------------------------------
# Торговый план
# ---------------------------------------------------------------------------

@dataclass
class TradePlan:
    """Конкретика сделки: где входить, где выходить, чем рисковать."""

    direction: str                       # long | short
    entry_low: float
    entry_high: float
    stop: float
    targets: list[float]
    target_rr: list[float]
    target_shares: list[int]
    max_leverage: float
    leverage_note: str
    rr: float
    rr_tp1: float
    invalidation: str
    horizon: str
    expected_bars: Optional[int]
    risk_percent_of_price: float
    position_percent_of_deposit: float
    risk_per_trade_percent: float
    basis: str                           # чем обоснован стоп

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0


def build_plan(
    direction: str,
    price: float,
    feats: dict[str, TimeframeFeatures],
    cfg: DirectionalConfig,
    profile: RiskProfile,
) -> Optional[TradePlan]:
    """
    Строит план от СТРУКТУРЫ, а не от «красивых» процентов:

      * стоп прячется за ближайший значимый экстремум ±0.6·ATR;
      * зона входа — коридор шириной ~0.5·ATR вокруг текущей цены,
        смещённый к ближайшему уровню (не «одна точка», в которую не попасть);
      * тейки — реальные уровни (ближайшие противоположные экстремумы), а если
        уровней нет — кратные риску (1R/2R/3.2R);
      * R/R считается по факту (TP2 против стопа), а не назначается заранее.
    """
    entry = feats.get(cfg.entry_tf)
    struct_tf = cfg.confirm_tfs[0] if (cfg.confirm_tfs and cfg.confirm_tfs[0] in feats) else cfg.entry_tf
    sf = feats.get(struct_tf)
    if entry is None or entry.atr is None or sf is None:
        return None
    atr = entry.atr
    if atr <= 0 or price <= 0:
        return None
    struct_atr = sf.atr or atr

    if direction == "long":
        support = sf.supports[0] if sf.supports else None
        entry_low = price - 0.45 * atr
        entry_high = price + 0.12 * atr
        if support and price - support < 1.2 * atr:
            entry_low = max(entry_low, support + 0.1 * atr)
        struct_stop = (support - 0.6 * struct_atr) if support else (price - 2.2 * atr)
        stop = min(struct_stop, entry_low - 0.9 * atr)
        basis = (
            f"за поддержкой {_fmt(support)} на {struct_tf} с запасом 0.6·ATR"
            if support else f"2.2·ATR под ценой (структурного уровня рядом нет)"
        )
        levels = [lvl for lvl in sf.resistances if lvl > price]
    elif direction == "short":
        resistance = sf.resistances[0] if sf.resistances else None
        entry_high = price + 0.45 * atr
        entry_low = price - 0.12 * atr
        if resistance and resistance - price < 1.2 * atr:
            entry_high = min(entry_high, resistance - 0.1 * atr)
        struct_stop = (resistance + 0.6 * struct_atr) if resistance else (price + 2.2 * atr)
        stop = max(struct_stop, entry_high + 0.9 * atr)
        basis = (
            f"за сопротивлением {_fmt(resistance)} на {struct_tf} с запасом 0.6·ATR"
            if resistance else f"2.2·ATR над ценой (структурного уровня рядом нет)"
        )
        levels = [lvl for lvl in sf.supports if lvl < price]
    else:
        return None

    if entry_low >= entry_high:
        entry_low, entry_high = min(entry_low, entry_high), max(entry_low, entry_high)
        if entry_low == entry_high:
            entry_high = entry_low * 1.0005
    entry_mid = (entry_low + entry_high) / 2.0
    risk = abs(entry_mid - stop)
    if risk <= 0:
        return None

    targets: list[float] = []
    sign = 1.0 if direction == "long" else -1.0
    for lvl in levels:
        dist = abs(lvl - entry_mid)
        if dist < risk * 0.7:
            continue                     # слишком близко — не тейк, а шум
        if dist > risk * 6.0:
            break
        targets.append(lvl - sign * 0.1 * atr)   # ставим чуть ДО уровня
        if len(targets) == 3:
            break
    fallback = [1.0, 2.0, 3.2]
    while len(targets) < 3:
        mult = fallback[len(targets)]
        candidate = entry_mid + sign * risk * mult
        if not targets or abs(candidate - targets[-1]) > risk * 0.4:
            targets.append(candidate)
        else:
            targets.append(targets[-1] + sign * risk * 0.8)
    targets = sorted(targets, reverse=(direction == "short"))

    target_rr = [abs(t - entry_mid) / risk for t in targets]
    rr = target_rr[1]
    rr_tp1 = target_rr[0]
    risk_pct = risk / entry_mid * 100.0
    position_pct = min(
        100.0, profile.risk_per_trade_percent / max(risk_pct, 1e-9) * 100.0
    )

    # Плечо считается ОТ СТОПА, а не «на глаз»: при плече L стоп в X% съедает
    # L×X% маржи. Ограничиваем так, чтобы срабатывание стопа стоило не больше
    # `max_stop_loss_of_margin` процентов маржи, и режем потолком профиля.
    raw_leverage = profile.max_stop_loss_of_margin / max(risk_pct, 1e-9)
    max_leverage = max(1.0, min(profile.max_leverage, math.floor(raw_leverage * 2) / 2))
    leverage_note = (
        f"стоп {risk_pct:.2f}% от входа; при {max_leverage:g}× его срабатывание "
        f"стоит {risk_pct * max_leverage:.1f}% маржи. Плечо НЕ увеличивает "
        f"вероятность успеха — только цену ошибки"
    )
    # частичная фиксация: чем дальше цель, тем меньше доля
    target_shares = [50, 30, 20]

    step = TIMEFRAME_SECONDS.get(cfg.entry_tf, 300)
    bars = int(math.ceil(abs(targets[0] - entry_mid) / atr)) if atr else None
    horizon_seconds = (bars or 0) * step
    if horizon_seconds <= 6 * 3600:
        horizon = f"скальп / внутри дня (~{_fmt_duration(horizon_seconds)} до TP1)"
    elif horizon_seconds <= 36 * 3600:
        horizon = f"внутри дня — сутки (~{_fmt_duration(horizon_seconds)} до TP1)"
    else:
        horizon = f"свинг на несколько дней (~{_fmt_duration(horizon_seconds)} до TP1)"

    invalidation = (
        f"закрытие свечи {struct_tf} ниже {_fmt(stop)} — идея сломана, выходим"
        if direction == "long" else
        f"закрытие свечи {struct_tf} выше {_fmt(stop)} — идея сломана, выходим"
    )

    return TradePlan(
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        targets=targets,
        target_rr=target_rr,
        target_shares=target_shares,
        max_leverage=max_leverage,
        leverage_note=leverage_note,
        rr=rr,
        rr_tp1=rr_tp1,
        invalidation=invalidation,
        horizon=horizon,
        expected_bars=bars,
        risk_percent_of_price=risk_pct,
        position_percent_of_deposit=position_pct,
        risk_per_trade_percent=profile.risk_per_trade_percent,
        basis=basis,
    )


def build_setup_plan(
    setup: SetupMatch,
    price: float,
    feats: dict[str, TimeframeFeatures],
    cfg: DirectionalConfig,
    profile: RiskProfile,
    anchor_tf: str = "1h",
) -> Optional[TradePlan]:
    """
    План по ПРОВЕРЕННОЙ геометрии сетапа, а не «на глаз».

    Стоп и цели заданы бэктестом (стоп 4·ATR, цели 1R/2R/3R). ATR берётся с
    часового таймфрейма — именно на нём сетапы проверялись, поэтому геометрия
    не «плывёт» от того, какой ТФ выбрал пользователь.
    """
    tf = anchor_tf if anchor_tf in feats else cfg.entry_tf
    f = feats.get(tf)
    if f is None or not f.atr or f.atr <= 0 or price <= 0:
        return None
    atr = f.atr
    sign = 1.0 if setup.side == "long" else -1.0

    # Зона входа: узкий коридор вокруг цены. Для лонга после обвала имеет смысл
    # брать чуть ниже рынка — на истории цена почти всегда ещё колеблется.
    if setup.side == "long":
        entry_low, entry_high = price - 0.35 * atr, price + 0.10 * atr
    else:
        entry_low, entry_high = price - 0.10 * atr, price + 0.35 * atr
    entry_mid = (entry_low + entry_high) / 2.0

    risk = setup.sl_atr * atr
    stop = entry_mid - sign * risk
    if stop <= 0:
        stop = entry_mid * 0.5
        risk = abs(entry_mid - stop)
    targets = [entry_mid + sign * r * risk for r in setup.tp_r]
    target_rr = [abs(t - entry_mid) / risk for t in targets]

    risk_pct = risk / entry_mid * 100.0
    position_pct = min(100.0, profile.risk_per_trade_percent / max(risk_pct, 1e-9) * 100.0)
    raw_leverage = profile.max_stop_loss_of_margin / max(risk_pct, 1e-9)
    max_leverage = max(1.0, min(profile.max_leverage, math.floor(raw_leverage * 2) / 2))
    leverage_note = (
        f"стоп широкий — {risk_pct:.1f}% от входа (так задумано: после обвала цена "
        f"сильно дёргается, узкий стоп выбьет до разворота). При {max_leverage:g}× "
        f"его срабатывание стоит {risk_pct * max_leverage:.1f}% маржи"
        if setup.side == "long" else
        f"стоп {risk_pct:.1f}% от входа; при {max_leverage:g}× его срабатывание "
        f"стоит {risk_pct * max_leverage:.1f}% маржи"
    )
    horizon = f"{setup.horizon_text} (сетап проверялся с удержанием до {setup.horizon_hours} часов)"
    return TradePlan(
        direction=setup.side,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        targets=targets,
        target_rr=target_rr,
        target_shares=list(setup.tp_shares),
        max_leverage=max_leverage,
        leverage_note=leverage_note,
        rr=target_rr[1] if len(target_rr) > 1 else target_rr[0],
        rr_tp1=target_rr[0],
        invalidation=setup.invalidation,
        horizon=horizon,
        expected_bars=int(setup.horizon_hours / max(1, TIMEFRAME_SECONDS.get(tf, 3600) / 3600)),
        risk_percent_of_price=risk_pct,
        position_percent_of_deposit=position_pct,
        risk_per_trade_percent=profile.risk_per_trade_percent,
        basis=f"{setup.sl_atr:g}·ATR({tf}) — ширина, на которой сетап проверен бэктестом",
    )


def build_setup_context(
    snap: MarketSnapshot,
    feats: dict[str, TimeframeFeatures],
    cfg: DirectionalConfig,
    anchor_tf: str = "1h",
) -> Optional[SetupContext]:
    """Собирает числа для сетапов по РЕАЛЬНЫМ свечам (без домыслов)."""
    tf = anchor_tf if anchor_tf in feats else cfg.entry_tf
    f = feats.get(tf)
    candles = snap.candles.get(tf)
    if f is None or candles is None or len(candles) < 30:
        return None
    price = snap.last_price or f.close
    if not price:
        return None

    step_h = max(1.0, TIMEFRAME_SECONDS.get(tf, 3600) / 3600.0)
    lookback = max(1, int(round(24.0 / step_h)))
    closes = list(candles.close)
    change_24h = None
    if len(closes) > lookback and closes[-1 - lookback] > 0:
        change_24h = closes[-1] / closes[-1 - lookback] - 1.0

    vols = [v for v in candles.volume if v is not None]
    volume_z = None
    if len(vols) >= 30:
        window = vols[-50:-1] if len(vols) >= 51 else vols[:-1]
        mean = sum(window) / len(window)
        var = sum((v - mean) ** 2 for v in window) / max(1, len(window) - 1)
        sd = var ** 0.5
        if sd > 0:
            volume_z = (vols[-1] - mean) / sd

    o, h, l, c = candles.open[-1], candles.high[-1], candles.low[-1], candles.close[-1]
    rng = h - l
    lower_wick = ((min(o, c) - l) / rng) if rng > 0 else None
    upper_wick = ((h - max(o, c)) / rng) if rng > 0 else None

    dist200 = None
    if f.ema_slow and f.atr:
        dist200 = (f.close - f.ema_slow) / f.atr

    return SetupContext(
        base=snap.base,
        price=float(price),
        atr=f.atr,
        atr_percent=f.atr_percent,
        change_24h=change_24h,
        volume_z=volume_z,
        volume_ratio=f.volume_ratio,
        market_change_24h=(snap.market_change_24h_percent / 100.0
                           if snap.market_change_24h_percent is not None else None),
        dollar_volume_24h=snap.quote_volume_24h,
        rsi=f.rsi,
        lower_wick=lower_wick,
        upper_wick=upper_wick,
        bb_width_rank=f.bb_width_rank,
        bb_position=f.bb_position,
        dist_ema200_atr=dist200,
        funding_rate=snap.funding_rate,
        base_timeframe=tf,
        bars=len(candles),
    )


# ---------------------------------------------------------------------------
# Итоговый сигнал
# ---------------------------------------------------------------------------

@dataclass
class DirectionalSignal:
    """Полный результат анализа монеты."""

    base: str
    exchange: str
    price: Optional[float]
    generated_at: float
    profile: RiskProfile
    direction: str                       # long | short | none
    actionable: bool
    data_confidence: float
    signal_confidence: float
    net_score: float
    agreement: float
    coverage: float
    factors: list[Factor] = field(default_factory=list)
    quality: DataQuality = field(default_factory=DataQuality)
    plan: Optional[TradePlan] = None
    why_not: list[str] = field(default_factory=list)
    explanation: str = ""
    carry_hint: Optional[str] = None
    timeframes_used: list[str] = field(default_factory=list)
    features: dict[str, "TimeframeFeatures"] = field(default_factory=dict)
    btc_correlation: Optional[float] = None
    listing_age_days: Optional[float] = None
    long_short_ratio: Optional[float] = None
    screen_rank: Optional[int] = None
    group_scores: dict[str, float] = field(default_factory=dict)
    agreeing_groups: int = 0
    entry_stretch_atr: Optional[float] = None
    setup: Optional[SetupMatch] = None
    setup_candidates: list[SetupMatch] = field(default_factory=list)
    context_score: float = 0.0          # голосование индикаторов — только как фон
    calibration: str = ""

    @property
    def direction_label(self) -> str:
        return {"long": "🟢 LONG", "short": "🔴 SHORT"}.get(self.direction, "⚪️ НЕТ СИГНАЛА")

    def confidence_breakdown(self) -> list[str]:
        """Из чего сложилась уверенность — для блока «почему»."""
        rows = [
            f"Данные: {self.data_confidence:.0f}%  ← " + ", ".join(
                f"{k} {v:.0f}" for k, v in self.quality.components.items()
            ),
            (
                f"Сигнал: {self.signal_confidence:.0f}%  ← сила факторов "
                f"|net|={abs(self.net_score):.2f}, согласие групп {self.agreement * 100:.0f}%, "
                f"независимых групп «за»: {self.agreeing_groups} "
                f"(потолок для такого числа групп — "
                f"{GROUP_CONFIDENCE_CAPS.get(self.agreeing_groups, 92.0):.0f}%), "
                f"покрытие {self.coverage * 100:.0f}%; сверху ограничено качеством данных"
            ),
        ]
        return rows

    def top_factors(self, limit: int = 6) -> list[Factor]:
        return sorted(self.factors, key=lambda f: abs(f.contribution), reverse=True)[:limit]


def analyze(
    snap: MarketSnapshot,
    *,
    profile: RiskProfile | str = DEFAULT_PROFILE,
    cfg: DirectionalConfig = DEFAULT_CONFIG,
    now: Optional[float] = None,
    carry_hint: Optional[str] = None,
) -> DirectionalSignal:
    """
    Главная функция: снапшот реальных данных → честный сигнал.

    Порядок ровно такой (и он важен):
      1) считаем индикаторы по закрытым барам;
      2) оцениваем КАЧЕСТВО данных — если плохо, дальше можно не считать;
      3) собираем факторы и их согласованность;
      4) только если факторы дружно смотрят в одну сторону — строим план;
      5) прогоняем план через ворота риск-профиля (R/R, уверенности);
      6) всё, что помешало, кладём в «почему НЕ вход».
    """
    now = now or time.time()
    prof = profile if isinstance(profile, RiskProfile) else RISK_PROFILES.get(
        str(profile), RISK_PROFILES[DEFAULT_PROFILE]
    )

    feats: dict[str, TimeframeFeatures] = {}
    for tf, candles in snap.candles.items():
        f = build_features(candles, cfg, now)
        if f is not None:
            feats[tf] = f

    quality = assess_data(snap, feats, cfg, prof, now)
    price = snap.last_price
    if price is None and cfg.entry_tf in feats:
        price = feats[cfg.entry_tf].close
    if price is None and snap.candles:
        any_tf = next(iter(snap.candles.values()))
        price = any_tf.close[-1] if len(any_tf) else None

    signal = DirectionalSignal(
        base=snap.base,
        exchange=snap.exchange,
        price=price,
        generated_at=now,
        profile=prof,
        direction="none",
        actionable=False,
        data_confidence=quality.score,
        signal_confidence=0.0,
        net_score=0.0,
        agreement=0.0,
        coverage=0.0,
        quality=quality,
        carry_hint=carry_hint,
        timeframes_used=sorted(feats, key=lambda t: TIMEFRAME_SECONDS.get(t, 0)),
        features=feats,
        btc_correlation=snap.btc_correlation,
        listing_age_days=snap.listing_age_days,
        long_short_ratio=snap.long_short_ratio,
    )

    if quality.blockers:
        signal.why_not.extend(quality.blockers)
        signal.explanation = (
            f"По {snap.base} сейчас нечего анализировать: "
            + "; ".join(quality.blockers)
            + ". Выдумывать сигнал на таких данных я не буду."
        )
        return signal

    factors = build_factors(snap, feats, cfg, carry_hint)
    signal.factors = factors
    if not factors:
        signal.why_not.append("не удалось посчитать ни одного фактора")
        signal.explanation = f"По {snap.base} не хватило данных даже на базовые индикаторы."
        return signal

    # Схлопываем факторы в НЕЗАВИСИМЫЕ группы и голосуем группами.
    groups: dict[str, list[Factor]] = {}
    for f in factors:
        groups.setdefault(f.category, []).append(f)
    group_scores: dict[str, tuple[float, float]] = {}
    for cat, items in groups.items():
        w = sum(i.weight for i in items)
        score = sum(i.contribution for i in items) / w if w else 0.0
        group_scores[cat] = (score, GROUP_WEIGHTS.get(cat, 1.0))

    total_weight = sum(w for _, w in group_scores.values())
    net = (
        sum(s * w for s, w in group_scores.values()) / total_weight
        if total_weight else 0.0
    )
    signal.net_score = net
    signal.group_scores = {c: s for c, (s, _) in group_scores.items()}

    active = [(s, w) for s, w in group_scores.values() if abs(s) >= 0.15]
    active_w = sum(w for _, w in active)
    agree_w = sum(w for s, w in active if _sign(s) == _sign(net) and net != 0)
    agreement = (agree_w / active_w) if active_w else 0.0
    signal.agreement = agreement

    coverage = _clamp(len(group_scores) / len(GROUP_WEIGHTS), 0.0, 1.0)
    signal.coverage = coverage
    signal.agreeing_groups = sum(
        1 for s, _ in group_scores.values()
        if abs(s) >= 0.15 and _sign(s) == _sign(net) and net != 0
    )

    strength = _clamp(abs(net) / cfg.net_saturation, 0.0, 1.0)
    raw_conf = 100.0 * (0.55 * strength + 0.45 * agreement) * (0.7 + 0.3 * coverage)
    raw_conf = min(
        raw_conf,
        GROUP_CONFIDENCE_CAPS.get(signal.agreeing_groups, cfg.max_signal_confidence),
    )

    # старший ТФ против направления → жёсткий потолок
    context = feats.get(cfg.context_tf)
    conflict = (
        context is not None
        and context.trend_score is not None
        and abs(context.trend_score) > 0.3
        and _sign(context.trend_score) != _sign(net)
        and net != 0
    )
    conf = min(raw_conf, quality.score, cfg.max_signal_confidence)
    if conflict:
        conf = min(conf, cfg.conflict_confidence_cap)
    signal.signal_confidence = _clamp(conf, 0.0, 100.0)

    # ------------------------------------------------------------------
    # РЕШЕНИЕ ПРИНИМАЮТ ПРОВЕРЕННЫЕ СЕТАПЫ, а не сумма голосов индикаторов.
    #
    # В версии v4 направление определялось знаком общего счёта факторов. На
    # 10 345 реальных сделках это дало PF 1.02 (примерно ноль), причём высокая
    # уверенность даже АНТИ-коррелировала с результатом. Поэтому теперь счёт
    # факторов — только фон/контекст, а сигнал выдаётся, лишь когда совпал
    # паттерн из проверенной библиотеки (`setups.py`).
    # ------------------------------------------------------------------
    signal.context_score = net
    ctx = build_setup_context(snap, feats, cfg)
    matches = detect_setups(ctx) if ctx is not None else []
    signal.setup_candidates = matches
    setup = matches[0] if matches else None
    signal.setup = setup

    if setup is None:
        signal.direction = "none"
        direction = "none"
        # без сетапа уверенности в прогнозе нет — показывать проценты нечестно
        signal.signal_confidence = 0.0
        if ctx is not None:
            misses = near_misses(ctx)
            signal.why_not.extend(misses)
            if not misses:
                signal.why_not.append(
                    "ни один из проверенных сетапов не сложился — рынок в обычном "
                    "состоянии, а торговать «просто так» статистически убыточно"
                )
        else:
            signal.why_not.append("не хватает данных, чтобы проверить сетапы")
    else:
        direction = setup.side
        signal.direction = direction
        # Уверенность = откалиброванная по истории оценка сетапа, ограниченная
        # сверху качеством данных (плохие данные не могут дать высокую уверенность).
        signal.signal_confidence = _clamp(
            min(setup.confidence, quality.score, cfg.max_signal_confidence), 0.0, 100.0
        )
        signal.calibration = calibration_note(signal.signal_confidence)
        plan = build_setup_plan(setup, float(price or 0.0), feats, cfg, prof)
        signal.plan = plan
        if plan is None:
            signal.why_not.append("не удалось построить план сделки (нет ATR на 1h)")
        if signal.signal_confidence < MIN_ACTIONABLE_CONFIDENCE:
            signal.why_not.append(
                f"уверенность {signal.signal_confidence:.0f}% ниже {MIN_ACTIONABLE_CONFIDENCE:.0f}% — "
                f"на истории у сигналов такого качества преимущества нет "
                f"(PF 0.91 против 1.40 у более уверенных)"
            )
        if setup.is_experimental and not prof.allow_experimental:
            signal.why_not.append(
                f"сетап «{setup.name}» помечен как экспериментальный "
                f"({setup.stats.summary() if setup.stats else 'мало данных'}), "
                f"а профиль «{prof.title}» такие не берёт — переключитесь на "
                f"🔥 агрессивный, если готовы"
            )

    # ---- ворота риск-профиля -------------------------------------------------
    if quality.score < prof.min_data_confidence:
        signal.why_not.append(
            f"уверенность в данных {quality.score:.0f}% ниже порога профиля "
            f"{prof.min_data_confidence:.0f}%"
        )
    if setup is not None and signal.signal_confidence < prof.min_signal_confidence:
        signal.why_not.append(
            f"уверенность {signal.signal_confidence:.0f}% ниже порога профиля "
            f"«{prof.title}» ({prof.min_signal_confidence:.0f}%)"
        )
    # Конфликт со старшим ТФ больше НЕ блокирует вход: сетап «разворот после
    # паники» по своей природе идёт против нисходящего тренда, и именно так он
    # проверен (PF 1.44-1.48). Оставляем как предупреждение в карточке.
    entry_feat = feats.get(cfg.entry_tf)
    if (
        direction != "none"
        and entry_feat is not None and entry_feat.ema_fast and entry_feat.atr
    ):
        stretch = (entry_feat.close - entry_feat.ema_fast) / entry_feat.atr
        signed = stretch if direction == "long" else -stretch
        signal.entry_stretch_atr = signed
        if cfg.max_entry_stretch_atr > 0 and signed > cfg.max_entry_stretch_atr:  # по умолчанию выключено
            signal.why_not.append(
                f"вход растянут: цена ушла на {signed:.1f}·ATR от EMA{cfg.ema_fast} "
                f"в сторону сигнала — это погоня за уже случившимся движением, "
                f"риск войти прямо перед откатом"
            )

    if quality.manipulation_flag and not prof.allow_manipulation_flag:
        signal.why_not.append(
            "обнаружены признаки аномального движения (памп/дамп или новостной вынос) — "
            "профиль запрещает вход в такие моменты"
        )
    if (
        quality.liquidity_usd is not None
        and quality.liquidity_usd < prof.min_quote_volume_24h
    ):
        signal.why_not.append(
            f"оборот ${quality.liquidity_usd / 1e6:.2f}M за 24ч ниже минимума профиля "
            f"${prof.min_quote_volume_24h / 1e6:.0f}M"
        )

    signal.actionable = bool(
        direction != "none" and signal.plan is not None and not signal.why_not
    )
    signal.explanation = _explain(signal, feats, cfg)
    return signal


# ---------------------------------------------------------------------------
# Человеческое объяснение
# ---------------------------------------------------------------------------

def _explain(
    signal: DirectionalSignal, feats: dict[str, TimeframeFeatures], cfg: DirectionalConfig
) -> str:
    """
    Человеческое объяснение. Сначала — что за ситуация (сетап), потом почему
    это статистически значимо, потом что конкретно делать.
    """
    base = signal.base
    if signal.setup is None:
        lead = f"{base}: ситуации, которую я умею отрабатывать, сейчас нет. "
        why = "; ".join(signal.why_not[:2]) if signal.why_not else "рынок в обычном состоянии"
        return (lead + why + ". Я специально не выдаю сигнал «просто чтобы был»: "
                "на истории входы без чёткого сетапа теряют деньги на комиссиях.")

    st = signal.setup
    parts = [
        f"{base}: сработал сетап «{st.name}» — {st.idea}."
    ]
    parts.append("Что именно совпало: " + "; ".join(st.evidence) + ".")
    if st.stats:
        parts.append(
            f"Честно о шансах: {st.stats.summary()}; на втором независимом наборе "
            f"данных — profit factor {st.stats_other.profit_factor:.2f}"
            if st.stats_other else f"Честно о шансах: {st.stats.summary()}."
        )
    if signal.calibration:
        parts.append(f"Про уверенность {signal.signal_confidence:.0f}%: {signal.calibration}.")
    if signal.plan:
        p = signal.plan
        parts.append(
            f"План: вход {_fmt(p.entry_low)}–{_fmt(p.entry_high)}, стоп {_fmt(p.stop)} "
            f"({p.risk_percent_of_price:.1f}% от входа), цели {_fmt(p.targets[0])} / "
            f"{_fmt(p.targets[1])}, удержание {st.horizon_text}."
        )
    if st.warnings:
        parts.append("Риски именно этого входа: " + "; ".join(st.warnings) + ".")
    if not signal.actionable and signal.why_not:
        parts.append("Почему я всё же не советую входить: " + "; ".join(signal.why_not) + ".")
    if signal.quality.warnings:
        parts.append("Осторожно: " + "; ".join(signal.quality.warnings[:2]) + ".")
    if signal.carry_hint:
        parts.append(signal.carry_hint)
    return " ".join(parts)


def beginner_breakdown(
    signal: "DirectionalSignal", cfg: DirectionalConfig = DEFAULT_CONFIG
) -> list[tuple[str, str]]:
    """
    Подробный разбор «как будто опытный трейдер объясняет новичку».
    Возвращает список (заголовок раздела, текст) — вью просто разложит по блокам.

    Разделы ровно те, что нужны новичку, чтобы понять СМЫСЛ, а не аббревиатуры:
      1) что происходит с монетой  4) почему такой стоп
      2) почему Long/Short         5) почему такие тейки
      3) почему эта точка входа    6) что может пойти не так
    """
    feats = signal.features
    entry = feats.get(cfg.entry_tf)
    struct_tf = cfg.confirm_tfs[0] if cfg.confirm_tfs and cfg.confirm_tfs[0] in feats else cfg.entry_tf
    sf = feats.get(struct_tf)
    plan = signal.plan
    out: list[tuple[str, str]] = []

    # 1. Что происходит
    bits: list[str] = []
    if sf and sf.structure_note:
        bits.append(
            f"на графике {struct_tf} сейчас {sf.structure_note}: "
            + (
                "каждый откат выкупают выше предыдущего — покупатель сильнее"
                if sf.structure > 0 else
                "каждый отскок продают ниже предыдущего — продавец сильнее"
                if sf.structure < 0 else
                "цена ходит в диапазоне, ни одна сторона не победила"
            )
        )
    if entry and entry.volume_ratio is not None:
        bits.append(
            f"объём последней свечи {cfg.entry_tf} — {entry.volume_ratio:.2f} от обычного"
            + (
                " (интерес к монете вырос — движение подкреплено деньгами)"
                if entry.volume_ratio > 1.3 else
                " (торгуют вяло — движению не на чем ехать)"
                if entry.volume_ratio < 0.7 else " (обычная активность)"
            )
        )
    if sf and sf.supports and sf.resistances:
        bits.append(
            f"цена зажата между поддержкой {_fmt(sf.supports[0])} и "
            f"сопротивлением {_fmt(sf.resistances[0])} — это границы текущей игры"
        )
    if entry and entry.round_level:
        bits.append(
            f"рядом круглый уровень {_fmt(entry.round_level)} — к таким числам цена "
            f"часто «липнет»: там висят чужие стопы и лимитки"
        )
    out.append(("1️⃣ Что происходит с монетой", ". ".join(bits) + "." if bits else "Данных для картины мало."))

    # 2. Почему Long/Short
    st = signal.setup
    if st is None:
        text = (
            "Направления нет, потому что не сложился ни один из паттернов, которые "
            "я умею отрабатывать. Я не выдаю сигнал из суммы мнений индикаторов: "
            "предыдущая версия бота так и делала, и на 10 345 реальных сделках это "
            "дало ноль (profit factor 1.02). Лучше подождать понятной ситуации."
        )
    else:
        side = "покупку (Long)" if signal.direction == "long" else "продажу (Short)"
        text = (
            f"Я предлагаю {side}, потому что сработал конкретный проверенный сетап "
            f"«{st.name}»: {st.idea}. Совпало вот что: " + "; ".join(st.evidence) + ". "
            + (f"Это не теория: {st.stats.summary()}." if st.stats else "")
        )
    out.append(("2️⃣ Почему именно так", text))

    # 3. Точка входа
    if plan:
        text = (
            f"Вход поставлен диапазоном {_fmt(plan.entry_low)}–{_fmt(plan.entry_high)}, "
            f"а не одной ценой, потому что рынок редко разворачивается точно в точке. "
            f"Диапазон шириной около половины средней свечи {cfg.entry_tf}: "
            f"если цена в него зайдёт — вы получите вход по средней цене, "
            f"а не будете гнаться за котировкой. "
            + (
                "Разумно входить частями: половина позиции по верхней границе, "
                "вторая — если цена даст лучшую цену."
            )
        )
    else:
        text = "Плана входа нет — сигнал не сформирован."
    out.append(("3️⃣ Почему такая точка входа", text))

    # 4. Стоп
    if plan and signal.setup is not None:
        text = (
            f"Стоп {_fmt(plan.stop)} — это {plan.basis}. Он специально ШИРОКИЙ "
            f"({plan.risk_percent_of_price:.1f}% от входа): после обвала цена ещё долго "
            f"дёргается, и узкий стоп выбьет вас прямо перед разворотом — это проверено, "
            f"с узким стопом (2.5·ATR) тот же сетап давал profit factor 0.99 вместо 1.44. "
            f"Широкий стоп НЕ значит большой риск: размер позиции считается так, чтобы "
            f"его срабатывание стоило ровно {plan.risk_per_trade_percent:.1f}% депозита."
        )
    elif plan:
        text = (
            f"Стоп {_fmt(plan.stop)} стоит {plan.basis}. Смысл: пока цена выше "
            f"(для лонга) этого уровня, идея жива; пробой означает, что рынок "
            f"передумал, и держать позицию «на надежде» — самый быстрый способ "
            f"потерять депозит. В деньгах это {plan.risk_percent_of_price:.2f}% "
            f"от входа, и именно от этого числа считается размер позиции и плечо."
        )
    else:
        text = "—"
    out.append(("4️⃣ Почему такой стоп", text))

    # 5. Тейки
    if plan:
        lines = []
        for i, (t, rr, share) in enumerate(
            zip(plan.targets, plan.target_rr, plan.target_shares), start=1
        ):
            lines.append(
                f"TP{i} {_fmt(t)} (R/R {rr:.2f}, фиксировать ~{share}% позиции)"
            )
        text = (
            "Цели стоят у ближайших уровней, где движение обычно тормозит: "
            + "; ".join(lines)
            + ". После TP1 стоп разумно перенести в безубыток — тогда худший "
            "сценарий превращается в ноль вместо минуса."
        )
    else:
        text = "—"
    out.append(("5️⃣ Почему такие цели", text))

    # 6. Риски
    risks: list[str] = []
    if signal.btc_correlation is not None and abs(signal.btc_correlation) > 0.7:
        risks.append(
            f"корреляция с BTC {signal.btc_correlation:+.2f} — если биткоин резко "
            f"пойдёт вниз, эта монета поедет за ним независимо от своего графика"
        )
    if signal.quality.liquidity_usd is not None and signal.quality.liquidity_usd < 20_000_000:
        risks.append(
            f"оборот всего ${signal.quality.liquidity_usd / 1e6:.1f}M за сутки — "
            f"в тонком рынке проскальзывание и «шпильки» съедают прибыль"
        )
    if signal.quality.manipulation_flag:
        risks.append("свежая аномальная свеча — возможен памп/дамп или новостной вынос")
    if entry and entry.atr_percent and entry.atr_percent > 1.0:
        risks.append(
            f"высокая волатильность ({entry.atr_percent:.2f}% средняя свеча "
            f"{cfg.entry_tf}) — стоп может выбить обычным шумом"
        )
    if signal.listing_age_days is not None and signal.listing_age_days < 60:
        risks.append(f"монета молодая (~{signal.listing_age_days:.0f} дней истории)")
    risks.append(
        "любые новости, регуляторика и движения BTC переигрывают любой график; "
        "торгуйте только тем, что готовы потерять"
    )
    out.append(("6️⃣ Что может пойти не так", "; ".join(risks) + "."))
    return out


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _sign(v: float) -> int:
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    av = abs(value)
    if av >= 1000:
        return f"{value:,.1f}".replace(",", " ")
    if av >= 1:
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if av >= 0.01:
        return f"{value:.5f}"
    return f"{value:.8f}".rstrip("0")


def _fmt_duration(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} мин"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} ч"
    return f"{seconds / 86400:.1f} дн"
