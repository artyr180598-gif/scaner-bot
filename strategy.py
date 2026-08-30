"""
strategy.py — квантовое ядро сканера (v3.0): «умный» сигнал вместо плоского порога.

ПРОБЛЕМА СТАРОГО ПОДХОДА (v2, плоский порог MIN_SPREAD_PERCENT=2%):
  * Реальные исполнимые чистые спреды на ликвидных парах почти всегда
    0.05%–0.8%. Порог 2% ловит только мусор/тихие книги → «мало сигналов».
  * NET считался как гросс − комиссии ВХОДА (0.15%). Но связку надо ещё
    ЗАКРЫТЬ (продать спот + выкупить перп) — ещё ~0.15% taker. Реальный
    профит завышен вдвое по комиссионной части.
  * Порог одинаков для всех пар, хотя у каждой пары своя статистика спреда:
    +0.5% для BTC — аномалия, для свежего мемкоина — обычное дело.

ЧТО ДЕЛАЕТ ЭТОТ МОДУЛЬ:
  1. SpreadSeries — короткая память пары: скользящее окно чистых спредов
     (mean/std/z-score/перцентиль) + оценка полужизни сходимости (OU/AR(1)).
  2. Funding edge — ожидаемый funding за горизонт удержания (шорт перпа
     получает funding при положительной ставке).
  3. Round-trip бухгалтерия: профит считается ПОСЛЕ входа И выхода.
  4. Persistence — сигнал подтверждён N наблюдениями подряд (анти-мерцание).
  5. Confidence 0–100 + грейд A/B/C/D + вердикт словами; actionable —
     строгие ворота входа.
  6. EpisodeTracker — живая статистика: как вели себя прошлые сигналы пары
     (winrate, среднее время удержания) — показывается в /stats.

ВАЖНО: модуль ЧИСТЫЙ (без сети, без ccxt) и используется ОДИН И ТОТ ЖЕ
код в живом сканере и в бектестере (backtest/engine.py) — бектест
проверяет ровно ту логику, что работает в бою. Никакого «отдельного
симуляционного» кода сигнала.

Ключевые идеи подтверждены исследованиями (см. AI_AGENTS/DECISIONS.md):
  * Makarov & Schoar (JFE 2020): межбиржевые спреды крупных, рекуррентных
    размеров живут дни-недели → mean-reversion с удержанием часы-дни реален.
  * Funding carry (long spot + short perp): 8–20%+ годовых при max DD < 2%
    (SSRN 2025, ScienceDirect 2025).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

__all__ = [
    "StrategyConfig",
    "SpreadStats",
    "SpreadSeries",
    "Assessment",
    "SignalEngine",
    "EpisodeTracker",
    "Episode",
]


# ---------------------------------------------------------------------------
# Конфигурация стратегии
# ---------------------------------------------------------------------------

#: Режимы работы двигателя сигнала.
STRATEGY_MODES: tuple[str, ...] = ("adaptive", "fixed")


@dataclass(frozen=True)
class StrategyConfig:
    """Параметры квантового ядра (все переопределяются переменными окружения)."""

    # adaptive — статистический вход (z-score/перцентиль + round-trip + funding);
    # fixed — легаси-режим v2: плоский порог min_spread_percent.
    mode: str = "adaptive"

    # --- fixed (легаси) -----------------------------------------------------
    min_spread_percent: float = 2.0

    # --- adaptive: ворота входа ----------------------------------------------
    #: минимальный ОЖИДАЕМЫЙ итог CARRY-сделки, %: спред − round-trip комиссии
    #: + funding за горизонт удержания. Это главный порог «стоит ли лезть».
    min_net_roundtrip_percent: float = 0.15
    #: минимальный вклад funding в ожидаемый итог, % (иначе это не carry,
    #: а чистая ставка на сходимость спреда — на ликвидных парах не работает)
    min_funding_edge_percent: float = 0.05
    #: класс REVERSION: минимальный чистый профит ПОСЛЕ round-trip комиссий
    #: при входе на аномалии спреда (без funding), %
    min_net_reversion_percent: float = 0.20
    #: включить класс CARRY (funding-сборщик)
    enable_carry: bool = True
    #: включить класс REVERSION (сходимость аномалий спреда)
    enable_reversion: bool = True
    #: z-score текущего спреда против истории пары: вход разрешён при
    #: z >= z_entry_min (не лезем, когда спред УЖЕ вывернут против нас).
    #: Строго положительный z_entry = «входи на аномалии» (кросс-биржевой
    #: режим), слабо отрицательный = «тайминг не против нас» (carry-режим).
    z_entry: float = 2.0
    z_entry_min: float = -1.0
    #: перцентиль текущего спреда в истории пары (0–100); 0 = не использовать
    pct_entry: float = 90.0
    #: подтверждений подряд (наблюдений/сканов), чтобы отсечь мерцание
    min_persistence: int = 3
    #: минимум наблюдений истории, прежде чем статистика вообще считается
    min_history: int = 40
    #: окно памяти пары, секунд (6 ч)
    history_seconds: float = 6.0 * 3600.0
    #: максимум сэмплов в окне (защита памяти; live-скан ~5 с → 6 ч = 4320)
    max_samples: int = 2000
    #: горизонт, за который начисляем ожидаемый funding, ч. Funding-режимы
    #: живут недели (SSRN/ScienceDirect 2025), поэтому для carry разумно
    #: 5–14 суток; консервативно — 168 ч (7 дней).
    horizon_hours: float = 168.0
    #: если оценённая полужизнь спреда выше этого (ч) — спред «не сходится»,
    #: это риск для выхода по сходимости. 0 = не проверять. Для carry
    #: проверка мягкая (выход всё равно по funding-флипу).
    max_halflife_hours: float = 0.0
    #: минимальная исполнимая глубина обеих ног, $ (иначе вход физически мал)
    min_fillable_usd: float = 100.0
    #: авто-пуш (SIGNAL_MODE=auto) только при confidence не ниже этого
    confidence_min_push: int = 65

    # --- комиссии (taker, % за сделку на ногу) -------------------------------
    spot_taker_fee_percent: float = 0.10
    futures_taker_fee_percent: float = 0.05

    # --- выход ----------------------------------------------------------------
    #: выходим, когда z спреда упал до этого уровня (0 = к среднему)
    z_exit: float = 0.0
    #: жёсткий тайм-стоп эпизода, ч (0 = без стопа по времени)
    max_episode_hours: float = 240.0

    @property
    def entry_fee_percent(self) -> float:
        """Комиссия входа в связку (обе ноги, taker), %."""
        return self.spot_taker_fee_percent + self.futures_taker_fee_percent

    @property
    def exit_fee_percent(self) -> float:
        """Комиссия выхода из связки (обе ноги, taker), %."""
        return self.spot_taker_fee_percent + self.futures_taker_fee_percent

    @property
    def roundtrip_fee_percent(self) -> float:
        """Полные комиссии вход+выход, % (вход уже вычтен в net_spread бота)."""
        return self.entry_fee_percent + self.exit_fee_percent

    @classmethod
    def from_settings(cls, settings: Any) -> "StrategyConfig":
        """Собирает конфиг из Settings сканера (env-переменные читает config.py)."""
        return cls(
            mode=settings.strategy_mode,
            min_spread_percent=settings.min_spread_percent,
            min_net_roundtrip_percent=settings.min_net_roundtrip_percent,
            min_funding_edge_percent=settings.min_funding_edge_percent,
            z_entry=settings.z_entry,
            z_entry_min=settings.z_entry_min,
            pct_entry=settings.pct_entry,
            min_persistence=settings.min_persistence,
            min_history=settings.min_history,
            history_seconds=settings.history_seconds,
            max_samples=settings.max_samples,
            horizon_hours=settings.funding_horizon_hours,
            max_halflife_hours=settings.max_halflife_hours,
            min_fillable_usd=settings.min_fillable_usd,
            confidence_min_push=settings.confidence_min_push,
            spot_taker_fee_percent=settings.spot_taker_fee_percent,
            futures_taker_fee_percent=settings.futures_taker_fee_percent,
            z_exit=settings.z_exit,
            max_episode_hours=settings.max_episode_hours,
        )


# ---------------------------------------------------------------------------
# Скользящая статистика спреда пары
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpreadStats:
    """Снимок статистики окна спредов пары."""

    n: int = 0                # наблюдений в окне
    mean: float = 0.0         # средний чистый спред, %
    std: float = 0.0          # станд. отклонение, п.п.
    span_seconds: float = 0.0 # глубина окна во времени
    min: float = 0.0
    max: float = 0.0
    halflife_hours: Optional[float] = None  # оценка AR(1)/OU, ч

    @property
    def ok(self) -> bool:
        return self.n > 0 and self.std > 0.0


class SpreadSeries:
    """
    Кольцевая память чистых спредов одной пары (ключ: монета+маршрут+направление).

    Пишется одним потоком (скан-цикл / шаг бектеста), читается при расчёте
    сигнала. Сэмплы — (ts_seconds, net_percent).

    Оптимизация: mean/std считаются скользящими суммами O(1), перцентиль —
    через поддержание отсортированного списка (bisect, C-скорость), полужизнь —
    по требованию O(n). Это позволяет гонять один и тот же код на 4м+ баров
    бектеста без тормозов.
    """

    __slots__ = ("_samples", "_sorted", "_sum", "_sumsq", "_max_samples",
                 "_window_seconds", "_dirty", "_cache")

    def __init__(
        self,
        max_samples: int = 2000,
        window_seconds: float = 6.0 * 3600.0,
    ) -> None:
        self._samples: Deque[tuple[float, float]] = deque(maxlen=max_samples)
        self._sorted: list[float] = []          # отсортированные значения окна
        self._sum = 0.0
        self._sumsq = 0.0
        self._max_samples = max_samples
        self._window_seconds = window_seconds
        self._dirty = True
        self._cache: SpreadStats = SpreadStats()

    # --- запись ---------------------------------------------------------------
    def add(self, ts: float, value: float) -> None:
        """Добавить наблюдение; окно само подрезается по времени и размеру."""
        if not math.isfinite(value):
            return
        import bisect
        samples = self._samples
        # при переполнении deque выталкивает слева сам — синхронизируем суммы
        if len(samples) == samples.maxlen:
            old_ts, old_v = samples[0]
            self._drop(old_ts, old_v, bisect)
        samples.append((float(ts), float(value)))
        bisect.insort(self._sorted, value)
        self._sum += value
        self._sumsq += value * value
        self._dirty = True
        cutoff = float(ts) - self._window_seconds
        while len(samples) > 1 and samples[0][0] < cutoff:
            old_ts, old_v = samples.popleft()
            self._drop(old_ts, old_v, bisect)

    def _drop(self, _ts: float, value: float, bisect: Any) -> None:
        idx = bisect.bisect_left(self._sorted, value)
        if idx < len(self._sorted):
            self._sorted.pop(idx)
        self._sum -= value
        self._sumsq -= value * value

    # --- чтение ---------------------------------------------------------------
    @property
    def last_value(self) -> Optional[float]:
        return self._samples[-1][1] if self._samples else None

    @property
    def last_ts(self) -> Optional[float]:
        return self._samples[-1][0] if self._samples else None

    def stats(self, compute_halflife: bool = False) -> SpreadStats:
        """mean/std/min/max (+полужизнь по требованию). O(1) без полужизни."""
        if not self._dirty:
            if not compute_halflife or self._cache.halflife_hours is not None:
                return self._cache
        samples = self._samples
        n = len(samples)
        if n == 0:
            self._cache = SpreadStats()
            self._dirty = False
            return self._cache
        mean = self._sum / n
        var = (self._sumsq - n * mean * mean) / max(1, n - 1)
        var = max(0.0, var)
        std = math.sqrt(var)
        span = samples[-1][0] - samples[0][0] if n > 1 else 0.0
        halflife = None
        if compute_halflife and n >= 8 and std > 0:
            halflife = self._halflife_hours([v for _, v in samples], span)
        self._cache = SpreadStats(
            n=n, mean=mean, std=std, span_seconds=span,
            min=self._sorted[0], max=self._sorted[-1],
            halflife_hours=halflife,
        )
        self._dirty = False
        return self._cache

    def zscore(self, value: float, stats: Optional[SpreadStats] = None) -> float:
        """Скольким сигмам истории соответствует значение (0 при плоской истории)."""
        st = stats if stats is not None else self.stats()
        if st.n < 2 or st.std <= 0.0:
            return 0.0
        return (value - st.mean) / st.std

    def percentile_rank(self, value: float) -> float:
        """Доля наблюдений окна ниже значения, 0–100 (точный bisect)."""
        if not self._sorted:
            return 0.0
        import bisect
        below = bisect.bisect_left(self._sorted, value)
        return 100.0 * below / len(self._sorted)

    # --- внутреннее -------------------------------------------------------------
    @staticmethod
    def _halflife_hours(values: list[float], span_seconds: float) -> Optional[float]:
        """
        Полужизнь сходимости спреда из AR(1): x_t = c + φ·x_{t-1} + ε.
        halflife = -ln(2) / ln(φ) в единицах шага; переводим в часы через
        средний интервал окна. φ<=0 (нет возврата к среднему) → None.
        """
        if len(values) < 8 or span_seconds <= 0:
            return None
        xs, ys = values[:-1], values[1:]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        den = sum((x - mean_x) ** 2 for x in xs)
        if den <= 0:
            return None
        phi = num / den
        if phi <= 0.0 or phi >= 1.0:
            return None
        step_hours = (span_seconds / max(1, n)) / 3600.0
        halflife_steps = -math.log(2.0) / math.log(phi)
        return halflife_steps * step_hours


# ---------------------------------------------------------------------------
# Оценка сигнала
# ---------------------------------------------------------------------------

@dataclass
class Assessment:
    """Результат оценки связки квантовым ядром."""

    key: str = ""
    # сырые входы
    net_spread_percent: float = 0.0          # как считает бот: гросс − комиссии входа
    gross_spread_percent: float = 0.0
    funding_rate_percent: Optional[float] = None  # %/8ч на перп-бирже (±)
    # статистика
    stats: SpreadStats = field(default_factory=SpreadStats)
    zscore: float = 0.0
    percentile: float = 0.0
    persistence: int = 0
    halflife_hours: Optional[float] = None
    # экономика
    funding_edge_percent: float = 0.0        # ожидаемый funding за горизонт, %
    roundtrip_net_percent: float = 0.0       # итог: вход − вход − выход + funding
    expected_hold_hours: Optional[float] = None
    # вердикт
    confidence: int = 0                      # 0–100
    grade: str = "D"                         # A/B/C/D
    signal_class: str = ""                   # CARRY | REVERSION | CARRY+REVERSION
    verdict: str = "—"                       # словами
    actionable: bool = False                 # строгие ворота входа
    reasons: list[str] = field(default_factory=list)  # почему да/нет

    # --- удобное -------------------------------------------------------------
    @property
    def is_strong(self) -> bool:
        return self.actionable and self.grade in ("A", "B")

    def describe_block(self) -> str:
        """Компактный текстовый блок для /signal (Telegram, mono)."""
        lines = [
            f"🧠 КВАНТОВАЯ ОЦЕНКА · {self.verdict}",
            f"Z-score        {self.zscore:+.2f}σ  (перцентиль {self.percentile:.0f} в истории пары)",
            f"Спред в истории среднее {self.stats.mean:+.3f}% · σ {self.stats.std:.3f} п.п. · n={self.stats.n}",
        ]
        if self.halflife_hours is not None:
            lines.append(
                f"Полужизнь спреда  ~{self.halflife_hours:.1f} ч (оценка AR(1)) · ожид. удержание "
                f"~{self.expected_hold_hours or self.halflife_hours:.1f} ч"
            )
        if self.funding_rate_percent is not None:
            lines.append(
                f"Funding edge    {self.funding_edge_percent:+.3f}% за {self.horizon_hours_g:.0f} ч "
                f"(ставка {self.funding_rate_percent:+.4f}%/8ч)"
            )
        lines.append(
            f"Round-trip NET  {self.roundtrip_net_percent:+.3f}% "
            f"(вход {self.net_spread_percent:+.3f}% − выход ~{self.exit_fee_g:.2f}% + funding)"
        )
        lines.append(
            f"Уверенность     {self.confidence}/100 · грейд {self.grade} · "
            f"подтверждений {self.persistence}"
        )
        if self.reasons:
            lines.append("Причины: " + "; ".join(self.reasons))
        return "\n".join(lines)

    # служебные поля для текста (заполняются движком)
    horizon_hours_g: float = 24.0
    exit_fee_g: float = 0.15


def funding_edge_percent(
    funding_rate_percent_per_8h: Optional[float],
    direction_spot_to_fut: bool,
    horizon_hours: float,
) -> float:
    """
    Ожидаемый funding за горизонт удержания, % от номинала.

    Знак: funding > 0 → лонги платят шортам.
      S→F (мы ШОРТ перпа) → получаем +funding;
      F→S (мы ЛОНГ перпа) → платим −funding (или получаем при отрицательной ставке).
    None → 0 (неизвестная ставка консервативно игнорируется).
    """
    if funding_rate_percent_per_8h is None:
        return 0.0
    accruals = horizon_hours / 8.0 * funding_rate_percent_per_8h
    return accruals if direction_spot_to_fut else -accruals


# ---------------------------------------------------------------------------
# Двигатель сигнала (единый для live и бектеста)
# ---------------------------------------------------------------------------

class SignalEngine:
    """
    Хранит память пар и выдаёт Assessment. Один и тот же экземпляр можно
    использовать в сканере (реальное время) и в бектестере (время баров) —
    логика входа идентична.

    Ключ пары: строка вида 'BTC|MEXC|Bybit|S>F' (монта|спот-биржа|перп-биржа|направление).
    """

    def __init__(self, config: StrategyConfig) -> None:
        self.cfg = config
        #: считать ли полужизнь AR(1) (для отображения; в бектесте выключено)
        self.want_halflife = config.max_halflife_hours > 0
        self._series: dict[str, SpreadSeries] = {}
        # сколько подряд наблюдений подряд выше порога (последний ts)
        self._above: dict[str, tuple[int, float]] = {}

    # --- память ----------------------------------------------------------------
    def series(self, key: str) -> SpreadSeries:
        got = self._series.get(key)
        if got is None:
            got = SpreadSeries(
                max_samples=self.cfg.max_samples,
                window_seconds=self.cfg.history_seconds,
            )
            self._series[key] = got
        return got

    def forget_stale(self, now: float, older_than_seconds: float) -> None:
        """Чистка памяти пар, по которым давно не было данных (гигиена ОЗУ)."""
        for key in [k for k, s in self._series.items() if s.last_ts is None or now - s.last_ts > older_than_seconds]:
            self._series.pop(key, None)
            self._above.pop(key, None)

    def assess_snapshot(
        self,
        key: str,
        net_spread_percent: float,
        gross_spread_percent: float,
        direction_spot_to_fut: bool,
        funding_rate_percent: Optional[float] = None,
        fillable_usd: float = 0.0,
        fresh: bool = True,
    ) -> Assessment:
        """
        Оценка СЕЙЧАС по уже накопленной истории пары — БЕЗ добавления сэмпла.

        Нужно, когда фоновый цикл кормит движок голыми спредами (без funding,
        он асинхронный), а команда /signal или авто-пуш хочет пересчитать
        оценку уже с funding-ставкой — не искажая историю и persistence.
        """
        cfg = self.cfg
        series = self._series.get(key)
        stats = series.stats() if series is not None else SpreadStats()
        z = series.zscore(net_spread_percent, stats) if series else 0.0
        pct = series.percentile_rank(net_spread_percent) if series else 0.0
        persistence = self._above.get(key, (0, 0.0))[0]
        history_ok = stats.n >= cfg.min_history and stats.std > 0.0

        f_edge = funding_edge_percent(
            funding_rate_percent, direction_spot_to_fut, cfg.horizon_hours
        )
        roundtrip_net = net_spread_percent - cfg.exit_fee_percent + f_edge
        spread_net_rt = net_spread_percent - cfg.exit_fee_percent

        carry_ok = (
            history_ok and cfg.enable_carry
            and f_edge >= cfg.min_funding_edge_percent
            and z >= cfg.z_entry_min
            and roundtrip_net >= cfg.min_net_roundtrip_percent
        )
        rev_ok = (
            history_ok and cfg.enable_reversion
            and (cfg.z_entry <= 0 or z >= cfg.z_entry)
            and (cfg.pct_entry <= 0 or pct >= cfg.pct_entry)
            and spread_net_rt >= cfg.min_net_reversion_percent
        )

        conf = 0.0
        if history_ok:
            conf += 10.0
        z_comp = max(-1.0, min(z, 4.0))
        conf += max(0.0, z_comp + 1.0) / 5.0 * 25.0
        conf += min(persistence, 6) / 6.0 * 10.0
        if fillable_usd >= 1000.0:
            conf += 10.0
        elif fillable_usd >= cfg.min_fillable_usd:
            conf += 10.0 * (fillable_usd - cfg.min_fillable_usd) / max(1e-9, 1000.0 - cfg.min_fillable_usd)
        f_comp = max(-1.0, min(1.0, f_edge / 0.30))
        conf += 20.0 * (f_comp + 1.0) / 2.0
        if roundtrip_net > 0:
            conf += 10.0 * min(1.0, roundtrip_net / 0.5)
        if not fresh:
            conf -= 15.0
        confidence = int(max(0, min(100, round(conf))))
        grade = "A" if confidence >= 80 else "B" if confidence >= 65 else "C" if confidence >= 50 else "D"

        actionable = (
            history_ok
            and persistence >= cfg.min_persistence
            and fillable_usd >= cfg.min_fillable_usd
            and fresh
            and (carry_ok or rev_ok)
        )
        if carry_ok and rev_ok:
            signal_class = "CARRY+REVERSION" if actionable else ""
        elif actionable and carry_ok:
            signal_class = "CARRY"
        elif actionable and rev_ok:
            signal_class = "REVERSION"
        else:
            signal_class = ""
        hold = stats.halflife_hours
        return Assessment(
            key=key,
            net_spread_percent=net_spread_percent,
            gross_spread_percent=gross_spread_percent,
            funding_rate_percent=funding_rate_percent,
            stats=stats,
            zscore=z,
            percentile=pct,
            persistence=persistence,
            halflife_hours=hold,
            funding_edge_percent=f_edge,
            roundtrip_net_percent=roundtrip_net,
            expected_hold_hours=min(hold, cfg.max_episode_hours) if hold else None,
            confidence=confidence,
            grade=grade,
            signal_class=signal_class,
            verdict=f"СИГНАЛ {signal_class}" if actionable else "нет входа",
            actionable=actionable,
            horizon_hours_g=cfg.horizon_hours,
            exit_fee_g=cfg.exit_fee_percent,
        )

    def observe_and_assess(
        self,
        key: str,
        ts: float,
        net_spread_percent: float,
        gross_spread_percent: float,
        direction_spot_to_fut: bool,
        funding_rate_percent: Optional[float] = None,
        fillable_usd: float = 0.0,
        fresh: bool = True,
    ) -> Assessment:
        """
        Обновляет историю пары и возвращает оценку СИГНАЛА прямо сейчас.

        net_spread_percent — чистый спред в семантике бота (гросс − комиссии
        входа), %; дальше движок сам вычитает выход и добавляет funding.
        """
        cfg = self.cfg
        series = self.series(key)
        series.add(ts, net_spread_percent)
        stats = series.stats(compute_halflife=self.want_halflife)

        z = series.zscore(net_spread_percent, stats)
        pct = series.percentile_rank(net_spread_percent)
        history_ok = stats.n >= cfg.min_history and stats.std > 0.0

        # --- экономика ------------------------------------------------------------
        f_edge = funding_edge_percent(
            funding_rate_percent, direction_spot_to_fut, cfg.horizon_hours
        )
        roundtrip_net = net_spread_percent - cfg.exit_fee_percent + f_edge
        spread_net_rt = net_spread_percent - cfg.exit_fee_percent

        # --- предварительные классы (для persistence и confidence) ---------------
        if cfg.mode == "fixed":
            interesting = net_spread_percent >= cfg.min_spread_percent
        else:
            carry_pre = (
                history_ok
                and cfg.enable_carry
                and f_edge >= cfg.min_funding_edge_percent
                and z >= cfg.z_entry_min
                and roundtrip_net >= cfg.min_net_roundtrip_percent
            )
            rev_pre = (
                history_ok
                and cfg.enable_reversion
                and (cfg.z_entry <= 0 or z >= cfg.z_entry)
                and (cfg.pct_entry <= 0 or pct >= cfg.pct_entry)
                and spread_net_rt >= cfg.min_net_reversion_percent
            )
            interesting = carry_pre or rev_pre

        # --- persistence: сколько подряд наблюдений «интересных» -----------------
        prev = self._above.get(key)
        if interesting:
            persistence = (prev[0] + 1) if (prev and ts - prev[1] <= 3 * cfg.history_seconds) else 1
            self._above[key] = (persistence, ts)
        else:
            persistence = 0
            self._above.pop(key, None)

        # ожидаемое время удержания: полужизнь AR(1), ограничена тайм-стопом
        hold = stats.halflife_hours
        expected_hold = min(hold, cfg.max_episode_hours) if hold else None

        # --- confidence 0–100 ----------------------------------------------------
        conf = 0.0
        if stats.n >= cfg.min_history:
            conf += 10.0
        z_comp = max(-1.0, min(z, 4.0))  # отрицательный z против нас — штраф
        conf += max(0.0, z_comp + 1.0) / 5.0 * 25.0
        conf += min(persistence, 6) / 6.0 * 10.0
        if fillable_usd >= 1000.0:
            conf += 10.0
        elif fillable_usd >= cfg.min_fillable_usd:
            conf += 10.0 * (fillable_usd - cfg.min_fillable_usd) / max(1e-9, 1000.0 - cfg.min_fillable_usd)
        # funding — главный мотор carry: ±0.30% за горизонт ≈ ±1
        f_comp = max(-1.0, min(1.0, f_edge / 0.30))
        conf += 20.0 * (f_comp + 1.0) / 2.0
        if roundtrip_net > 0:
            conf += 10.0 * min(1.0, roundtrip_net / 0.5)
        if not fresh:
            conf -= 15.0
        confidence = int(max(0, min(100, round(conf))))
        grade = "A" if confidence >= 80 else "B" if confidence >= 65 else "C" if confidence >= 50 else "D"

        # --- ворота входа: два класса сигнала ------------------------------------
        reasons: list[str] = []
        history_ok = stats.n >= cfg.min_history and stats.std > 0.0
        spread_net_rt = net_spread_percent - cfg.exit_fee_percent  # сходимость без funding

        if cfg.mode == "fixed":
            actionable = net_spread_percent >= cfg.min_spread_percent
            signal_class = "LEGACY" if actionable else ""
            if not actionable:
                reasons.append(f"чистый спред {net_spread_percent:.3f}% < порога {cfg.min_spread_percent:.2f}%")
            verdict = "плоский порог (легаси-режим)"
        else:
            carry_ok = False
            rev_ok = False
            if history_ok:
                if cfg.enable_carry:
                    if f_edge < cfg.min_funding_edge_percent:
                        reasons.append(
                            f"CARRY: funding edge {f_edge:.3f}% < {cfg.min_funding_edge_percent:.2f}%"
                        )
                    elif z < cfg.z_entry_min:
                        reasons.append(f"CARRY: z {z:.2f}σ < {cfg.z_entry_min:.1f}σ (спред вывернут против нас)")
                    elif roundtrip_net < cfg.min_net_roundtrip_percent:
                        reasons.append(
                            f"CARRY: ожидаемый итог {roundtrip_net:.3f}% < {cfg.min_net_roundtrip_percent:.2f}%"
                        )
                    else:
                        carry_ok = True
                if cfg.enable_reversion:
                    if cfg.z_entry > 0 and z < cfg.z_entry:
                        reasons.append(f"REV: z {z:.2f}σ < {cfg.z_entry:.1f}σ (не аномалия)")
                    elif spread_net_rt < cfg.min_net_reversion_percent:
                        reasons.append(
                            f"REV: сходимость после комиссий {spread_net_rt:.3f}% < "
                            f"{cfg.min_net_reversion_percent:.2f}%"
                        )
                    else:
                        rev_ok = True
            else:
                reasons.append(f"мало истории ({stats.n}/{cfg.min_history}) — статистика не готова")
            if not history_ok:
                actionable = False
            elif persistence < cfg.min_persistence:
                actionable = False
                reasons.append(f"подтверждений {persistence}/{cfg.min_persistence} (анти-мерцание)")
            elif cfg.max_halflife_hours > 0 and stats.halflife_hours is not None and stats.halflife_hours > cfg.max_halflife_hours:
                actionable = False
                reasons.append(f"полужизнь {stats.halflife_hours:.0f} ч > {cfg.max_halflife_hours:.0f} ч — спред не сходится")
            elif fillable_usd < cfg.min_fillable_usd:
                actionable = False
                reasons.append(f"глубина ${fillable_usd:.0f} < ${cfg.min_fillable_usd:.0f}")
            elif not fresh:
                actionable = False
                reasons.append("котировки несвежие")
            else:
                actionable = carry_ok or rev_ok
            if carry_ok and rev_ok:
                signal_class = "CARRY+REVERSION" if actionable else ""
            elif carry_ok:
                signal_class = "CARRY" if actionable else ""
            elif rev_ok:
                signal_class = "REVERSION" if actionable else ""
            else:
                signal_class = ""
            verdict = f"СИГНАЛ {signal_class}" if actionable else "нет входа"

        return Assessment(
            key=key,
            net_spread_percent=net_spread_percent,
            gross_spread_percent=gross_spread_percent,
            funding_rate_percent=funding_rate_percent,
            stats=stats,
            zscore=z,
            percentile=pct,
            persistence=persistence,
            halflife_hours=stats.halflife_hours,
            funding_edge_percent=f_edge,
            roundtrip_net_percent=roundtrip_net,
            expected_hold_hours=expected_hold,
            confidence=confidence,
            grade=grade,
            signal_class=signal_class,
            verdict=verdict,
            actionable=actionable,
            reasons=reasons,
            horizon_hours_g=cfg.horizon_hours,
            exit_fee_g=cfg.exit_fee_percent,
        )


# ---------------------------------------------------------------------------
# Живой трекер эпизодов (для /stats: winrate сигналов пары)
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    key: str
    opened_at: float
    entry_net_percent: float
    entry_z: float
    funding_rate_percent: Optional[float]
    closed_at: Optional[float] = None
    exit_net_percent: Optional[float] = None
    pnl_percent: Optional[float] = None     # оценка захваченного схождения
    status: str = "open"                    # open | closed


class EpisodeTracker:
    """
    Отслеживает «эпизоды» пар: сигнал открыт → спред сходится → закрыт.
    Даёт живой winrate и среднее время удержания — то, чего бектестом
    нельзя пренебречь при живой работе.

    Оценка PnL консервативная: захваченное схождение спреда
    (entry_net − exit_net) − комиссии выхода, funding не начисляем
    (не знаем ставку на выходе) → реально чуть лучше.
    """

    def __init__(self, exit_fee_percent: float, z_exit: float = 0.0,
                 max_episode_hours: float = 240.0) -> None:
        self.exit_fee_percent = exit_fee_percent
        self.z_exit = z_exit
        self.max_episode_hours = max_episode_hours
        self._open: dict[str, Episode] = {}
        self._closed: deque[Episode] = deque(maxlen=500)

    # --- API ------------------------------------------------------------------
    def update(self, assessment: Assessment, now: float) -> None:
        """Прокормить трекер оценкой; сам откроет/закроет эпизоды."""
        a = assessment
        ep = self._open.get(a.key)
        if ep is None:
            if a.actionable:
                self._open[a.key] = Episode(
                    key=a.key,
                    opened_at=now,
                    entry_net_percent=a.net_spread_percent,
                    entry_z=a.zscore,
                    funding_rate_percent=a.funding_rate_percent,
                )
            return
        # эпизод открыт: условие выхода
        age_hours = (now - ep.opened_at) / 3600.0
        converged = a.zscore <= self.z_exit or a.net_spread_percent <= 0.0
        timed_out = self.max_episode_hours > 0 and age_hours >= self.max_episode_hours
        if converged or timed_out:
            ep.closed_at = now
            ep.exit_net_percent = a.net_spread_percent
            ep.pnl_percent = (
                (ep.entry_net_percent - ep.exit_net_percent) - self.exit_fee_percent
            )
            ep.status = "closed" if ep.pnl_percent > 0 else "closed_loss"
            self._closed.append(ep)
            self._open.pop(a.key, None)
            # если сразу снова сигнал — откроем на следующем update
        elif a.actionable:
            # сигнал продолжает держаться — обновляем «лучший» вход не нужно,
            # держим первый (честно: вошли бы один раз)
            pass

    # --- статистика --------------------------------------------------------------
    def summary(self) -> dict[str, float]:
        closed = list(self._closed)
        if not closed:
            return {"episodes": 0, "open": len(self._open), "winrate": 0.0,
                    "avg_pnl_percent": 0.0, "avg_hold_hours": 0.0}
        wins = [e for e in closed if (e.pnl_percent or 0.0) > 0.0]
        holds = [(e.closed_at - e.opened_at) / 3600.0 for e in closed if e.closed_at]
        return {
            "episodes": len(closed),
            "open": len(self._open),
            "winrate": 100.0 * len(wins) / len(closed),
            "avg_pnl_percent": sum(e.pnl_percent or 0.0 for e in closed) / len(closed),
            "avg_hold_hours": sum(holds) / len(holds) if holds else 0.0,
        }

    def best_pairs(self, limit: int = 10) -> list[tuple[str, int, float, float]]:
        """(key, эпизодов, winrate%, средний pnl%) — топ пар по истории."""
        agg: dict[str, list[Episode]] = {}
        for e in self._closed:
            agg.setdefault(e.key, []).append(e)
        rows = []
        for key, eps in agg.items():
            wins = sum(1 for e in eps if (e.pnl_percent or 0.0) > 0.0)
            rows.append((
                key,
                len(eps),
                100.0 * wins / len(eps),
                sum(e.pnl_percent or 0.0 for e in eps) / len(eps),
            ))
        rows.sort(key=lambda r: (r[3], r[2]), reverse=True)
        return rows[:limit]

    @property
    def open_episodes(self) -> list[Episode]:
        return list(self._open.values())


# ---------------------------------------------------------------------------
# typing shim (config импортируется только для типов, без цикла)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - типизация только
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from config import Settings
        Any = object  # noqa: F811 (заглушка для type checkers)
except Exception:  # pragma: no cover
    pass
