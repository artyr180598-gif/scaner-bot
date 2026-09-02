"""
app/analysis/base.py — контракт модулей анализа («тентакли»).

Архитектурная идея позаимствована у OctoBot (tentacles: каждый оценщик
возвращает «оценочную заметку», стратегия их комбинирует) и freqtrade
(``populate_indicators`` → общие колонки для всех правил), но доведена до
жёсткого контракта:

    модуль = чистая функция  MarketFeatures → список[Factor]

Модуль НЕ знает про скоринг, Telegram, сеть. Чтобы добавить новый вид анализа,
достаточно написать функцию и повесить ``@analysis_module("имя", "группа")`` —
реестр подхватит её автоматически (``app/analysis/registry.py``).

Важно про группы: зависимые факторы обязаны иметь ОДНУ группу. Тренд на трёх
таймфреймах — это одно наблюдение, а не три голоса; иначе уверенность
надувается на боковике (проверено на бектесте, см. AI_AGENTS/BRAIN.md v4-0d).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Protocol, runtime_checkable

from app.domain.models import (Candles, DerivativesInfo, Factor, MarketContext,
                               MarketSnapshot, Timeframe, TickerInfo)

__all__ = ["AnalysisModule", "DIRECTIONAL_GROUPS", "Group", "Level",
           "MarketFeatures", "ModuleFunc", "TimeframeIndicators", "run_modules"]
from app.indicators.profile import VolumeProfile
from app.indicators.structure import StructureState
from app.indicators.volatility import VolatilityState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Группы факторов
# ---------------------------------------------------------------------------

class Group:
    TREND = "trend"
    MOMENTUM = "momentum"
    STRUCTURE = "structure"
    SMC = "smc"
    VOLUME = "volume"
    LEVELS = "levels"
    DERIVATIVES = "derivatives"
    CONTEXT = "context"
    SENTIMENT = "sentiment"
    # Ненаправленные группы: не голосуют «за/против», а повышают ПОТЕНЦИАЛ
    # движения и качество сигнала (см. scoring/scorer.py).
    POTENTIAL = "potential"
    QUALITY = "quality"


DIRECTIONAL_GROUPS = (
    Group.TREND, Group.MOMENTUM, Group.STRUCTURE, Group.SMC,
    Group.VOLUME, Group.LEVELS, Group.DERIVATIVES, Group.CONTEXT,
    Group.SENTIMENT,
)


# ---------------------------------------------------------------------------
# Индикаторы одного таймфрейма (посчитаны один раз — переиспользуются модулями)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class TimeframeIndicators:
    """Всё, что нужно знать про один таймфрейм. `candles` — исходник."""

    timeframe: Timeframe
    candles: Candles
    # последние значения
    rsi: float = float("nan")
    macd: float = float("nan")
    macd_signal: float = float("nan")
    macd_hist: float = float("nan")
    macd_hist_prev: float = float("nan")
    ema20: float = float("nan")
    ema50: float = float("nan")
    ema100: float = float("nan")
    ema200: float = float("nan")
    adx: float = float("nan")
    plus_di: float = float("nan")
    minus_di: float = float("nan")
    stoch_k: float = float("nan")
    stoch_d: float = float("nan")
    bb_upper: float = float("nan")
    bb_lower: float = float("nan")
    bb_mid: float = float("nan")
    bb_percent_b: float = float("nan")
    atr: float = float("nan")
    atr_pct: float = float("nan")
    mfi: float = float("nan")
    cci: float = float("nan")
    williams_r: float = float("nan")
    obv_slope_pct: float = float("nan")   # наклон OBV за 20 баров, %
    volume_z: float = float("nan")        # z-score последнего объёма
    volume_ratio: float = float("nan")    # объём / средняя
    roc_short: float = float("nan")       # % за 10 баров
    roc_long: float = float("nan")        # % за 30 баров
    slope_pct: float = float("nan")       # наклон регрессии, %/бар
    trend_r2: float = float("nan")        # качество тренда 0..1
    supertrend_dir: float = float("nan")
    vwap: float = float("nan")
    choppiness: float = float("nan")
    # ряды (для дивергенций и структурных проверок)
    rsi_series: Optional[object] = None
    close_series: Optional[object] = None
    macd_hist_series: Optional[object] = None
    bars: int = 0

    @property
    def price(self) -> float:
        return self.candles.last_price

    @property
    def usable(self) -> bool:
        return self.bars >= 30 and self.rsi == self.rsi  # NaN-проверка

    def ema_stack(self) -> str:
        """up | down | mixed — расположение EMA относительно друг друга."""
        vals = [v for v in (self.ema20, self.ema50, self.ema100, self.ema200)
                if v == v]  # отбрасываем NaN
        if len(vals) < 2:
            return "mixed"
        if all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)):
            return "up"
        if all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)):
            return "down"
        return "mixed"


# ---------------------------------------------------------------------------
# Совокупность признаков по монете
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Level:
    """Значимый ценовой уровень (поддержка/сопротивление)."""

    price: float
    kind: str            # "support" | "resistance"
    source: str          # "swing" | "poc" | "vah" | "val" | "ob" | "fvg" | "round"
    strength: float = 0.5
    touches: int = 1

    def distance_pct(self, price: float) -> float:
        return (price / self.price - 1) * 100


@dataclass(slots=True)
class MarketFeatures:
    """
    Полный набор признаков по одной монете: то, что едят модули анализа.

    Собирается один раз (``app/analysis/features.py``) и переиспользуется —
    иначе каждый модуль пересчитывал бы EMA/ADX по кругу.
    """

    snapshot: MarketSnapshot
    signal_tf: Timeframe
    indicators: Dict[Timeframe, TimeframeIndicators] = field(default_factory=dict)
    volatility: Dict[Timeframe, VolatilityState] = field(default_factory=dict)
    structure: Dict[Timeframe, StructureState] = field(default_factory=dict)
    profile: Optional[VolumeProfile] = None
    levels: List[Level] = field(default_factory=list)
    context: MarketContext = field(default_factory=MarketContext)
    # производные метрики (для скрининга и объяснений)
    metrics: Dict[str, float] = field(default_factory=dict)
    flags: Dict[str, bool] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    # -- доступы ------------------------------------------------------------
    def ind(self, tf: Timeframe) -> Optional[TimeframeIndicators]:
        return self.indicators.get(tf)

    def primary(self) -> TimeframeIndicators:
        """
        Индикаторы сигнального таймфрейма (или младшего доступного).

        Если данных нет вовсе — возвращаем пустую заглушку, а не падаем:
        вызывающий код обязан корректно обработать «данных нет»
        (см. ``build_plan`` → None и WAIT-сигнал).
        """
        if self.signal_tf in self.indicators:
            return self.indicators[self.signal_tf]
        if self.indicators:
            return self.indicators[min(self.indicators, key=lambda t: t.minutes)]
        return TimeframeIndicators(
            timeframe=self.signal_tf,
            candles=Candles.from_raw(self.symbol, self.signal_tf, []),
        )

    def vol(self, tf: Timeframe) -> Optional[VolatilityState]:
        return self.volatility.get(tf)

    def struct(self, tf: Timeframe) -> Optional[StructureState]:
        return self.structure.get(tf)

    @property
    def price(self) -> float:
        return self.snapshot.price

    @property
    def symbol(self) -> str:
        return self.snapshot.symbol

    @property
    def ticker(self) -> Optional[TickerInfo]:
        return self.snapshot.ticker

    @property
    def derivatives(self) -> DerivativesInfo:
        return self.snapshot.derivatives

    @property
    def atr(self) -> float:
        return self.primary().atr

    def higher_tfs(self) -> List[Timeframe]:
        """Таймфреймы старше сигнального, от младшего к старшему."""
        return sorted(
            (tf for tf in self.indicators if tf.minutes > self.signal_tf.minutes),
            key=lambda t: t.minutes,
        )

    def nearest_level(self, side: str, max_distance_atr: float = 6.0
                      ) -> Optional[Level]:
        """Ближайший уровень нужного типа (support/resistance) в пределах N ATR."""
        atr = self.atr
        if not atr or atr != atr:
            return None
        candidates = [
            lv for lv in self.levels
            if lv.kind == side and abs(lv.distance_pct(self.price)) * self.price / 100
            <= max_distance_atr * atr
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda lv: abs(lv.price - self.price))


# ---------------------------------------------------------------------------
# Контракт модуля
# ---------------------------------------------------------------------------

@runtime_checkable
class AnalysisModule(Protocol):
    name: str
    group: str

    def __call__(self, features: MarketFeatures) -> Iterable[Factor]: ...


ModuleFunc = Callable[[MarketFeatures], Iterable[Factor]]


def run_modules(
    modules: Iterable[AnalysisModule],
    features: MarketFeatures,
    logger: logging.Logger = log,
) -> List[Factor]:
    """
    Прогон всех модулей. Падение одного модуля не роняет анализ: пишем
    предупреждение в notes и продолжаем (важно для живого бота).
    """
    factors: List[Factor] = []
    for module in modules:
        try:
            produced = module(features)
            if produced:
                factors.extend(list(produced))
        except Exception as exc:  # noqa: BLE001 — устойчивость важнее чистоты
            logger.warning("модуль %s упал: %s", getattr(module, "name", module), exc,
                           exc_info=True)
            features.notes.append(f"модуль {getattr(module, 'name', module)} отключён")
    return factors
