"""
app/domain/models.py — доменные модели (без внешних зависимостей кроме stdlib).

Здесь живут «существительные» системы: свеча, снимок рынка, фактор, сигнал,
план сделки. Ни один класс этого модуля не знает ни про Telegram, ни про ccxt —
это позволяет гонять ядро на синтетике, в бектесте и в unit-тестах.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Перечисления
# ---------------------------------------------------------------------------


class Timeframe(str, Enum):
    """Таймфреймы. `minutes` используется для ресемплинга и горизонта."""

    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def minutes(self) -> int:
        return _TF_MINUTES[self.value]

    @property
    def pandas_freq(self) -> str:
        return f"{self.minutes}min"

    @classmethod
    def parse(cls, value: str) -> "Timeframe":
        raw = (value or "").strip().lower()
        aliases = {
            "5m": cls.M5, "15m": cls.M15, "30m": cls.M30,
            "1h": cls.H1, "60m": cls.H1, "4h": cls.H4, "240m": cls.H4,
            "1d": cls.D1, "24h": cls.D1, "day": cls.D1,
        }
        if raw not in aliases:
            raise ValueError(f"Неизвестный таймфрейм: {value!r}")
        return aliases[raw]


_TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

# Порядок «от младшего к старшему» — нужен для мульти-ТФ анализа.
TF_ORDER: List[Timeframe] = [
    Timeframe.M5, Timeframe.M15, Timeframe.M30,
    Timeframe.H1, Timeframe.H4, Timeframe.D1,
]


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"

    @property
    def emoji(self) -> str:
        return {"LONG": "🚀", "SHORT": "🔻", "WAIT": "⏸"}[self.value]

    @property
    def sign(self) -> int:
        return {"LONG": 1, "SHORT": -1, "WAIT": 0}[self.value]


class RiskProfile(str, Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"

    @classmethod
    def parse(cls, value: str) -> "RiskProfile":
        raw = (value or "").strip().lower()
        for item in cls:
            if item.value == raw or item.name.lower() == raw:
                return item
        raise ValueError(f"Неизвестный риск-профиль: {value!r}")


# ---------------------------------------------------------------------------
# Свечи
# ---------------------------------------------------------------------------

OHLCV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


@dataclass(slots=True)
class Candles:
    """
    Набор свечей одного символа и одного таймфрейма.

    Внутри — pandas.DataFrame с колонками
    ``timestamp, open, high, low, close, volume`` и DatetimeIndex (UTC).
    Последний бар может быть НЕЗАКРЫТЫМ: флаг ``last_closed`` говорит,
    можно ли принимать по нему решения (в бектесте — никогда, в live — только
    для «свежести» котировки).
    """

    symbol: str
    timeframe: Timeframe
    df: pd.DataFrame
    last_closed: bool = True
    source: str = ""

    def __post_init__(self) -> None:
        if not set(OHLCV_COLUMNS).issubset(self.df.columns):
            missing = set(OHLCV_COLUMNS) - set(self.df.columns)
            raise ValueError(f"Candles: не хватает колонок {sorted(missing)}")
        self.df = self.df.copy()
        if not isinstance(self.df.index, pd.DatetimeIndex):
            self.df.index = pd.to_datetime(self.df["timestamp"], utc=True)
        if self.df.index.tz is None:  # pragma: no cover — защита от наивных ts
            self.df.index = self.df.index.tz_localize("UTC")
        # Дубли и разрывы порядка ломают все скользящие расчёты.
        self.df = self.df[~self.df.index.duplicated(keep="last")].sort_index()
        for col in ("open", "high", "low", "close", "volume"):
            self.df[col] = pd.to_numeric(self.df[col], errors="coerce")
        self.df = self.df.dropna(subset=["close"])

    # -- удобные доступы ----------------------------------------------------
    def __len__(self) -> int:
        return len(self.df)

    @property
    def empty(self) -> bool:
        return len(self.df) == 0

    @property
    def close(self) -> pd.Series:
        return self.df["close"]

    @property
    def high(self) -> pd.Series:
        return self.df["high"]

    @property
    def low(self) -> pd.Series:
        return self.df["low"]

    @property
    def open(self) -> pd.Series:
        return self.df["open"]

    @property
    def volume(self) -> pd.Series:
        return self.df["volume"]

    @property
    def last_price(self) -> float:
        return float(self.df["close"].iloc[-1]) if len(self.df) else float("nan")

    @property
    def last_timestamp(self) -> datetime:
        if len(self.df) == 0:
            raise ValueError("Candles: пустой набор")
        ts = self.df.index[-1]
        return ts.to_pydatetime()

    @property
    def last_close_timestamp(self) -> datetime:
        """
        Момент закрытия последней свечи.

        Биржи (ccxt) метят свечу временем её ОТКРЫТИЯ, поэтому часовая свеча,
        закрытая минуту назад, помечена временем «час назад».
        """
        return self.last_timestamp + timedelta(minutes=self.timeframe.minutes)

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        """
        Сколько секунд прошло с ЗАКРЫТИЯ последней свечи.

        Именно так нужно мерить свежесть: для часового ТФ последняя закрытая
        свеча всегда «открыта» час назад, и возраст от открытия признавал бы
        устаревшими любые часовые данные (гейт свежести отвергал бы все
        сигналы подряд).
        """
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.last_close_timestamp).total_seconds())

    def head(self, n: int) -> "Candles":
        """Обрезка слева (для бектеста: «история на момент t»)."""
        return Candles(self.symbol, self.timeframe, self.df.iloc[:n],
                       last_closed=self.last_closed, source=self.source)

    def tail(self, n: int) -> "Candles":
        return Candles(self.symbol, self.timeframe, self.df.iloc[-n:],
                       last_closed=self.last_closed, source=self.source)

    def resample(self, target: Timeframe) -> "Candles":
        """
        Построение старшего таймфрейма из младшего (идея freqtrade
        `merge_informative_pair`, только без лишних сетевых запросов).
        """
        if target.minutes <= self.timeframe.minutes:
            raise ValueError(
                f"Нельзя собрать {target.value} из {self.timeframe.value}")
        if target.minutes % self.timeframe.minutes:
            raise ValueError(
                f"{target.value} не кратен {self.timeframe.value}")
        bucket = target.pandas_freq
        agg = self.df.resample(bucket, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        )
        # Хвостовое окно почти всегда неполное: часовой ряд в 20:47 даёт
        # «четырёхчасовку», в которой всего 47 минут. Анализировать её как
        # закрытую — значит смотреть на несуществующий бар (и получать разные
        # результаты в разное время суток). неполный хвост отбрасываем.
        ratio = target.minutes // self.timeframe.minutes
        counts = self.df.resample(bucket, label="left", closed="left").size()
        if len(counts) and int(counts.iloc[-1]) < ratio:
            agg = agg.iloc[:-1]
        # Незаполненные окна (пропуски торгов) убираем, хвост без данных — тоже.
        agg = agg.dropna(subset=["open", "close"])
        # Если исходный последний бар был незакрытым, производный — тоже.
        out = agg.reset_index().rename(columns={"index": "timestamp"})
        if "timestamp" not in out.columns:  # pandas 3 может назвать иначе
            out = out.rename(columns={out.columns[0]: "timestamp"})
        return Candles(self.symbol, target, out,
                       last_closed=self.last_closed, source=self.source)

    @classmethod
    def from_raw(
        cls,
        symbol: str,
        timeframe: Timeframe | str,
        rows: Sequence[Sequence[float]],
        source: str = "",
        last_closed: bool = True,
    ) -> "Candles":
        """
        Сборка из «сырого» ответа ccxt: [[ts_ms, o, h, l, c, v], ...].
        """
        tf = timeframe if isinstance(timeframe, Timeframe) else Timeframe.parse(timeframe)
        if not rows:
            empty_index = pd.DatetimeIndex([], dtype="datetime64[ns, UTC]")
            empty = pd.DataFrame(
                {"timestamp": empty_index,
                 "open": pd.Series(dtype="float64"),
                 "high": pd.Series(dtype="float64"),
                 "low": pd.Series(dtype="float64"),
                 "close": pd.Series(dtype="float64"),
                 "volume": pd.Series(dtype="float64")},
                index=empty_index,
            )
            return cls(symbol, tf, empty, last_closed=last_closed, source=source)
        arr = np.asarray(rows, dtype="float64")
        if arr.ndim != 2 or arr.shape[1] < 6:
            raise ValueError(f"Ожидался массив Nx6, получен {arr.shape}")
        idx = pd.to_datetime(arr[:, 0], unit="ms", utc=True)
        df = pd.DataFrame(
            {
                "timestamp": idx,
                "open": arr[:, 1], "high": arr[:, 2], "low": arr[:, 3],
                "close": arr[:, 4], "volume": arr[:, 5],
            },
            index=idx,
        )
        return cls(symbol, tf, df, last_closed=last_closed, source=source)


# ---------------------------------------------------------------------------
# Факторы (результат работы модулей анализа)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Factor:
    """
    Один аргумент «за» или «против».

    ``score`` в диапазоне [-1, 1]: +1 — максимально в пользу LONG,
    -1 — максимально в пользу SHORT, 0 — нейтрально/нет данных.
    ``group`` группирует зависимые факторы (тренд на 3 ТФ — это ОДНО
    наблюдение, а не три голоса; см. AI_AGENTS/BRAIN.md, урок v4-0d).
    """

    name: str
    group: str
    score: float
    evidence: str
    weight: float = 1.0
    detail: str = ""

    def __post_init__(self) -> None:
        if math.isnan(self.score):
            self.score = 0.0
        self.score = float(max(-1.0, min(1.0, self.score)))
        self.weight = float(max(0.0, self.weight))

    @property
    def aligned(self) -> bool:
        return abs(self.score) >= 0.15

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "group": self.group, "score": round(self.score, 3),
            "weight": self.weight, "evidence": self.evidence, "detail": self.detail,
        }


@dataclass(slots=True)
class FactorSet:
    """Набор факторов + служебная информация о качестве данных."""

    factors: List[Factor] = field(default_factory=list)
    data_confidence: float = 1.0
    notes: List[str] = field(default_factory=list)

    def add(self, factor: Optional[Factor]) -> None:
        if factor is not None:
            self.factors.append(factor)

    def extend(self, factors: Iterable[Factor]) -> None:
        for f in factors:
            self.add(f)

    def by_group(self) -> Dict[str, List[Factor]]:
        out: Dict[str, List[Factor]] = {}
        for f in self.factors:
            out.setdefault(f.group, []).append(f)
        return out

    def get(self, name: str) -> Optional[Factor]:
        for f in self.factors:
            if f.name == name:
                return f
        return None


# ---------------------------------------------------------------------------
# Снимок рынка
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TickerInfo:
    """24-часовая статистика тикера (для скрининга)."""

    symbol: str
    last: float = float("nan")
    quote_volume: float = 0.0     # оборот в котирующей валюте за 24 ч
    base_volume: float = 0.0
    change_pct: float = 0.0       # изменение за 24 ч, %
    high: float = float("nan")
    low: float = float("nan")
    bid: float = float("nan")
    ask: float = float("nan")

    @property
    def spread_pct(self) -> float:
        if not (self.bid and self.ask) or math.isnan(self.bid) or math.isnan(self.ask):
            return float("nan")
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100 if mid else float("nan")


@dataclass(slots=True)
class DerivativesInfo:
    """Фьючерсная обвязка: funding, open interest, их динамика."""

    funding_rate: Optional[float] = None        # доля за период (0.0001 = 0.01%)
    funding_interval_hours: int = 8
    open_interest: Optional[float] = None       # в контрактах/базовой валюте
    open_interest_value: Optional[float] = None  # в USDT
    open_interest_change_pct: Optional[float] = None  # за 24 ч
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    available: bool = False

    @property
    def funding_pct(self) -> Optional[float]:
        if self.funding_rate is None:
            return None
        return self.funding_rate * 100

    @property
    def basis_pct(self) -> Optional[float]:
        """Премия/дисконт марки к индексу — прокси перегрева лонгов."""
        if not self.mark_price or not self.index_price:
            return None
        return (self.mark_price / self.index_price - 1) * 100


@dataclass(slots=True)
class MarketSnapshot:
    """
    Всё, что известно о монете на момент анализа.

    ``candles`` — словарь таймфрейм → Candles; базовый (самый младший) ТФ
    обязателен, старшие могут быть получены ресемплингом.
    """

    symbol: str
    base: str
    quote: str
    exchange: str
    candles: Dict[Timeframe, Candles] = field(default_factory=dict)
    ticker: Optional[TickerInfo] = None
    derivatives: DerivativesInfo = field(default_factory=DerivativesInfo)
    universe_stats: Dict[str, float] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: List[str] = field(default_factory=list)
    # Новостной/социальный фон (опциональный источник, может отсутствовать).
    sentiment: Optional[float] = None       # -1..1, None = данных нет
    news: List[str] = field(default_factory=list)

    # -- доступы ------------------------------------------------------------
    def tf(self, timeframe: Timeframe) -> Optional[Candles]:
        return self.candles.get(timeframe)

    def primary_tf(self) -> Timeframe:
        """Младший доступный таймфрейм — «рабочий» для сигнала."""
        if not self.candles:
            raise ValueError(f"{self.symbol}: нет свечей в снимке")
        return min(self.candles, key=lambda t: t.minutes)

    @property
    def price(self) -> float:
        tf = self.primary_tf()
        return self.candles[tf].last_price

    def bars(self, timeframe: Timeframe) -> int:
        c = self.candles.get(timeframe)
        return len(c) if c else 0

    def staleness_seconds(self) -> float:
        tf = self.primary_tf()
        return self.candles[tf].age_seconds(self.fetched_at)


# ---------------------------------------------------------------------------
# План сделки
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Target:
    price: float
    label: str = "TP1"
    basis: str = ""            # чем обоснована цель («ближайший OB», «2×ATR»…)
    fraction: float = 0.0      # доля позиции к фиксации (0..1)
    pct: float = float("nan")  # % от средней точки входа — заполняет TradePlan
    rr: float = float("nan")   # R к этой цели — заполняет TradePlan


@dataclass(slots=True)
class TradePlan:
    """
    Готовый план: зона входа, стоп, 3 цели, R:R, размер позиции, плечо.

    Все цены абсолютные; проценты считаются от средней точки входа.
    """

    direction: Direction
    entry_low: float
    entry_high: float
    stop: float
    targets: List[Target]
    entry_basis: str = ""
    stop_basis: str = ""
    invalidation: float = float("nan")
    horizon_hours: int = 24
    atr: float = float("nan")
    risk_pct_of_deposit: float = 1.0
    max_leverage: float = 5.0

    # вычисляемые
    entry_mid: float = field(init=False)
    stop_pct: float = field(init=False)
    risk_per_unit: float = field(init=False)
    rr_primary: float = field(init=False)
    rr_avg: float = field(init=False)
    target_pcts: List[float] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.entry_mid = (self.entry_low + self.entry_high) / 2
        self.risk_per_unit = abs(self.entry_mid - self.stop)
        self.stop_pct = (self.stop / self.entry_mid - 1) * 100 if self.entry_mid else 0.0
        pcts: List[float] = []
        for t in self.targets:
            pct = (t.price / self.entry_mid - 1) * 100 if self.entry_mid else 0.0
            t.pct = pct
            t.rr = (abs(t.price - self.entry_mid) / self.risk_per_unit
                    if self.risk_per_unit else float("nan"))
            pcts.append(pct)
        self.target_pcts = pcts
        rewards = [abs(t.price - self.entry_mid) for t in self.targets]
        self.rr_primary = rewards[0] / self.risk_per_unit if self.risk_per_unit else 0.0
        self.rr_avg = (sum(rewards) / len(rewards) / self.risk_per_unit
                       if rewards and self.risk_per_unit else 0.0)

    # -- проверки корректности ---------------------------------------------
    def is_valid(self) -> bool:
        if self.direction is Direction.WAIT:
            return True
        if not all(map(_finite, (self.entry_low, self.entry_high, self.stop, self.entry_mid))):
            return False
        if self.risk_per_unit <= 0:
            return False
        if self.direction is Direction.LONG:
            if not (self.entry_low <= self.entry_high):
                return False
            if self.stop >= self.entry_low:
                return False
            if self.targets and self.targets[0].price <= self.entry_high:
                return False
            prices = [t.price for t in self.targets]
            if prices != sorted(prices):
                return False
        else:
            if not (self.entry_low <= self.entry_high):
                return False
            if self.stop <= self.entry_high:
                return False
            if self.targets and self.targets[0].price >= self.entry_low:
                return False
            prices = [t.price for t in self.targets]
            if prices != sorted(prices, reverse=True):
                return False
        return all(_finite(p) and p > 0 for p in [t.price for t in self.targets])

    def position_size(self, deposit: float) -> Dict[str, float]:
        """
        Размер позиции по стопу (классика риск-менеджмента из jesse/freqtrade):
        notional = deposit * risk% / stop_distance%.
        """
        if not self.risk_per_unit or not _finite(self.entry_mid):
            return {"notional": 0.0, "units": 0.0, "risk_usd": 0.0,
                    "stop_pct": 0.0, "leverage": 1.0}
        stop_pct = self.risk_per_unit / self.entry_mid
        risk_usd = deposit * self.risk_pct_of_deposit / 100
        notional = risk_usd / stop_pct if stop_pct > 0 else 0.0
        units = notional / self.entry_mid if self.entry_mid else 0.0
        return {
            "notional": notional,
            "units": units,
            "risk_usd": risk_usd,
            "stop_pct": stop_pct * 100,
            # Плечо не увеличивает риск (он задан стопом), оно лишь экономит
            # маржу. Ограничение — пользовательский потолок и 1/stop_pct
            # (иначе ликвидация наступит раньше стопа).
            "leverage": min(self.max_leverage, 1.0 / stop_pct) if stop_pct > 0 else 1.0,
        }


def _finite(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(float(x)) and float(x) > 0


# ---------------------------------------------------------------------------
# Сигнал
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Signal:
    """
    Итог анализа одной монеты.

    ``confidence`` — 0..10. Это **сила согласия независимых групп аргументов**,
    а НЕ вероятность прибыли (проверено на 10k сделок: «уверенность» не
    предсказывает успех, см. AI_AGENTS/BRAIN.md). Поэтому рядом всегда
    отдаётся ``data_confidence`` и честная расшифровка.
    """

    symbol: str
    direction: Direction
    confidence: float
    score: float                       # -1..1, знак = направление
    timeframe: Timeframe
    plan: Optional[TradePlan]
    factors: FactorSet
    summary: str                       # «почему именно эта монета и сейчас»
    setup: str = ""                    # название сетапа («сжатие + спрос у OB»)
    exchange: str = ""
    price: float = float("nan")
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tags: List[str] = field(default_factory=list)
    data_confidence: float = 1.0
    horizon_hours: int = 24
    metrics: Dict[str, float] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.direction is not Direction.WAIT and self.plan is not None \
            and self.plan.is_valid()

    @property
    def rr(self) -> float:
        return self.plan.rr_primary if self.plan else 0.0

    def key(self) -> str:
        return f"{self.symbol}:{self.timeframe.value}:{self.direction.value}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "direction": self.direction.value,
            "confidence": round(self.confidence, 2), "score": round(self.score, 3),
            "timeframe": self.timeframe.value, "setup": self.setup,
            "price": self.price, "summary": self.summary,
            "tags": list(self.tags), "data_confidence": round(self.data_confidence, 3),
            "rr": round(self.rr, 2),
            "plan": {
                "entry_low": self.plan.entry_low, "entry_high": self.plan.entry_high,
                "stop": self.plan.stop,
                "targets": [t.price for t in self.plan.targets],
            } if self.plan else None,
            "created_at": self.created_at.isoformat(),
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
        }


# ---------------------------------------------------------------------------
# Промежуточные структуры скрининга
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PrescreenCandidate:
    """Монета, прошедшая пре-скрининг «сжатой пружины»."""

    symbol: str
    base: str
    score: float
    ticker: TickerInfo
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    exchange: str = ""


@dataclass(slots=True)
class MarketContext:
    """Общий фон рынка (BTC + широта) — влияет на вес альт-сигналов."""

    btc_direction: Direction = Direction.WAIT
    btc_score: float = 0.0
    btc_trend: str = "флэт"
    breadth_above_ema: float = 0.5     # доля вселенной выше EMA50, 0..1
    breadth_24h_positive: float = 0.5
    median_change_24h: float = 0.0
    regime: str = "нейтральный"
    alt_multiplier: float = 1.0        # 0.6..1.15
    notes: List[str] = field(default_factory=list)

    def describe_ru(self) -> str:
        arrow = {"LONG": "▲ бычий", "SHORT": "▼ медвежий", "WAIT": "◆ боковой"}[
            self.btc_direction.value]
        return (
            f"BTC: {arrow} ({self.btc_trend}) · "
            f"ширина рынка: {self.breadth_24h_positive * 100:.0f}% монет в плюсе за 24ч · "
            f"медиана {self.median_change_24h:+.1f}%"
        )


@dataclass(slots=True)
class ScanReport:
    """Итог одного прогона сканера."""

    signals: List[Signal] = field(default_factory=list)
    candidates: List[PrescreenCandidate] = field(default_factory=list)
    context: MarketContext = field(default_factory=MarketContext)
    universe_size: int = 0
    scanned: int = 0
    duration_seconds: float = 0.0
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    errors: List[str] = field(default_factory=list)

    @property
    def actionable(self) -> List[Signal]:
        return [s for s in self.signals if s.actionable]

    def top(self, n: int = 10) -> List[Signal]:
        return sorted(self.actionable, key=lambda s: (-s.confidence, -s.rr))[:n]
