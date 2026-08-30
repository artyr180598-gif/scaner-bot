"""
alpha.py — направленный слой v4 «PULSE».

Это НЕ арбитраж (тот остаётся в strategy.py). Здесь — оценка,
КУДА с большей вероятностью пойдёт цена монеты на горизонте
нескольких баров. Используется ОДИН И ТОТ ЖЕ код в живом сканере
(/pulse) и в бектесте (backtest/run_pulse.py).

ЧЕСТНОСТЬ:
  * Никакого заглядывания в будущее: сигнал по закрытию бара t,
    исполнение — по OPEN бара t+1.
  * Классические индикаторы (RSI/MACD/EMA/BB) — это БЕЙЗЛАЙНЫ.
    Собственный продукт — композит PULSE: режим рынка + crowding
    (funding/basis) + нормированный моментум + короткий mean-reversion.
  * Модуль чистый: без сети, без pandas, без numpy.

Почему не «просто RSI»: на реальных крипто-рядах одиночные TA-сигналы
после комиссий обычно около нуля (это проверяется бектестом). Edge,
который имеет шанс выжить — кросс-секционный отбор и crypto-native
crowding (экстремальный funding предсказывает разгрузку плеча).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "PulseConfig",
    "PulseSnapshot",
    "PulseEngine",
    "rsi_last",
    "ema_last",
    "atr_last",
    "adx_last",
    "bollinger_last",
    "macd_last",
    "zscore_last",
    "tanh_clip",
]


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PulseConfig:
    """Параметры направленного движка (подобраны на IS, проверяются на OOS)."""

    # память
    max_bars: int = 400
    min_bars: int = 60

    # индикаторы
    rsi_n: int = 14
    atr_n: int = 14
    adx_n: int = 14
    ema_fast: int = 12
    ema_slow: int = 26
    bb_n: int = 20
    bb_k: float = 2.0
    mom_fast: int = 6          # ~1 день на 4ч
    mom_slow: int = 42         # ~1 неделя на 4ч
    funding_z_n: int = 90      # ~15 дней funding
    basis_z_n: int = 90

    # режим: ADX выше порога = тренд, иначе флэт
    adx_trend: float = 22.0

    # ворота сигнала
    score_entry: float = 0.38  # |score| для входа
    score_exit: float = 0.12   # |score| для выхода (гистерезис)
    crowding_extreme: float = 1.8  # |funding z| — жёсткий fade

    # веса композита (тренд)
    w_mom: float = 0.42
    w_crowd_trend: float = 0.28
    w_basis_trend: float = 0.18
    w_vol_trend: float = 0.12

    # веса композита (флэт / mean-reversion)
    w_mr: float = 0.36
    w_rsi: float = 0.22
    w_crowd_range: float = 0.28
    w_basis_range: float = 0.14


# ---------------------------------------------------------------------------
# Снимок оценки
# ---------------------------------------------------------------------------

@dataclass
class PulseSnapshot:
    """Направленная оценка одной монеты прямо сейчас."""

    symbol: str = ""
    n_bars: int = 0
    close: float = 0.0
    # индикаторы
    rsi: Optional[float] = None
    adx: Optional[float] = None
    atr_pct: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    macd_hist: Optional[float] = None
    bb_pct: Optional[float] = None          # 0 = нижняя лента, 1 = верхняя
    mom_fast: Optional[float] = None        # доходность, доля
    mom_slow: Optional[float] = None
    vol_norm_mom: Optional[float] = None    # mom_slow / (σ√n)
    ret_1: Optional[float] = None
    funding_pct: Optional[float] = None     # %/8ч
    funding_z: Optional[float] = None
    basis_pct: Optional[float] = None
    basis_z: Optional[float] = None
    volume_z: Optional[float] = None
    # композит
    regime: str = "—"                       # TREND | RANGE | —
    score: float = 0.0                      # −1…+1  (+ = лонг)
    confidence: int = 0                     # 0–100
    side: int = 0                           # +1 long, −1 short, 0 flat
    grade: str = "D"
    reasons: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.side != 0 and self.grade in ("A", "B")

    def direction_label(self) -> str:
        if self.side > 0:
            return "ЛОНГ"
        if self.side < 0:
            return "ШОРТ"
        return "НЕТ СИГНАЛА"

    def describe_block(self) -> str:
        side = self.direction_label()
        lines = [
            f"📡 PULSE · {side} · score {self.score:+.2f} · {self.regime} · {self.grade}",
            f"Уверенность  {self.confidence}/100 · баров {self.n_bars}",
        ]
        if self.rsi is not None:
            lines.append(f"RSI {self.rsi:.1f} · ADX {self.adx or 0:.1f} · ATR {self.atr_pct or 0:.2f}%")
        if self.vol_norm_mom is not None:
            lines.append(
                f"Моментум {self.mom_slow * 100 if self.mom_slow is not None else 0:+.2f}% "
                f"(vol-norm {self.vol_norm_mom:+.2f})"
            )
        if self.funding_z is not None:
            lines.append(
                f"Crowding  funding z {self.funding_z:+.2f}σ "
                f"({self.funding_pct:+.4f}%/8ч)" if self.funding_pct is not None
                else f"Crowding  funding z {self.funding_z:+.2f}σ"
            )
        if self.basis_z is not None:
            lines.append(f"Базис z {self.basis_z:+.2f}σ ({self.basis_pct:+.3f}%)" if self.basis_pct is not None else f"Базис z {self.basis_z:+.2f}σ")
        if self.reasons:
            lines.append("Причины: " + "; ".join(self.reasons))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Математика индикаторов (последнее значение по массиву close/high/low)
# ---------------------------------------------------------------------------

def tanh_clip(x: float, scale: float = 1.0) -> float:
    """Мягкий клип в (−1, 1)."""
    if not math.isfinite(x) or scale == 0:
        return 0.0
    return math.tanh(x / scale)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(max(0.0, var))


def zscore_last(values: list[float], n: int) -> Optional[float]:
    if len(values) < max(8, n // 3):
        return None
    window = values[-n:] if len(values) >= n else values
    s = _std(window)
    if s <= 0:
        return 0.0
    return (window[-1] - _mean(window)) / s


def rsi_last(closes: list[float], n: int = 14) -> Optional[float]:
    """Wilder RSI, последнее значение."""
    if len(closes) < n + 1:
        return None
    gains = []
    losses = []
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains) / n
    avg_l = sum(losses) / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(ch, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-ch, 0.0)) / n
    if avg_l <= 1e-18:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def ema_last(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2.0 / (n + 1.0)
    ema = _mean(values[:n])
    for v in values[n:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def atr_last(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1 or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < n:
        return None
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def adx_last(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> Optional[float]:
    """ADX Уайлдера, последнее значение. None если мало баров."""
    m = len(closes)
    if m < 2 * n + 1 or len(highs) != m or len(lows) != m:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, m):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    # сглаживание Уайлдера
    def wilder(xs: list[float]) -> list[float]:
        out = [sum(xs[:n])]
        for x in xs[n:]:
            out.append(out[-1] - out[-1] / n + x)
        return out

    atr_s = wilder(trs)
    p_s = wilder(plus_dm)
    m_s = wilder(minus_dm)
    dx: list[float] = []
    for i in range(len(atr_s)):
        atr = atr_s[i]
        if atr <= 0:
            dx.append(0.0)
            continue
        pdi = 100.0 * p_s[i] / atr
        mdi = 100.0 * m_s[i] / atr
        s = pdi + mdi
        dx.append(0.0 if s <= 0 else 100.0 * abs(pdi - mdi) / s)
    if len(dx) < n:
        return None
    adx = sum(dx[:n]) / n
    for v in dx[n:]:
        adx = (adx * (n - 1) + v) / n
    return adx


def bollinger_last(closes: list[float], n: int = 20, k: float = 2.0) -> Optional[float]:
    """Положение цены в канале Боллинджера: 0 = нижняя, 1 = верхняя."""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = _mean(window)
    sd = _std(window)
    if sd <= 0:
        return 0.5
    lo, hi = mid - k * sd, mid + k * sd
    width = hi - lo
    if width <= 0:
        return 0.5
    return (closes[-1] - lo) / width


def macd_last(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[float]:
    """Последняя гистограмма MACD (macd − signal)."""
    if len(closes) < slow + signal:
        return None
    k_f = 2.0 / (fast + 1.0)
    k_s = 2.0 / (slow + 1.0)
    k_sig = 2.0 / (signal + 1.0)
    ema_f = closes[0]
    ema_s = closes[0]
    macd_line: list[float] = []
    for v in closes:
        ema_f = v * k_f + ema_f * (1.0 - k_f)
        ema_s = v * k_s + ema_s * (1.0 - k_s)
        macd_line.append(ema_f - ema_s)
    if len(macd_line) < signal:
        return None
    sig = _mean(macd_line[:signal])
    hist = macd_line[signal - 1] - sig
    for v in macd_line[signal:]:
        sig = v * k_sig + sig * (1.0 - k_sig)
        hist = v - sig
    return hist


# ---------------------------------------------------------------------------
# Движок
# ---------------------------------------------------------------------------

class _Tape:
    """Кольцевая память OHLCV + funding/basis одной монеты."""

    __slots__ = ("ts", "open", "high", "low", "close", "volume", "funding", "basis")

    def __init__(self, max_bars: int) -> None:
        self.ts: deque[float] = deque(maxlen=max_bars)
        self.open: deque[float] = deque(maxlen=max_bars)
        self.high: deque[float] = deque(maxlen=max_bars)
        self.low: deque[float] = deque(maxlen=max_bars)
        self.close: deque[float] = deque(maxlen=max_bars)
        self.volume: deque[float] = deque(maxlen=max_bars)
        self.funding: deque[float] = deque(maxlen=max_bars)
        self.basis: deque[float] = deque(maxlen=max_bars)

    def add(
        self,
        ts: float,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 0.0,
        funding_pct: Optional[float] = None,
        basis_pct: Optional[float] = None,
    ) -> None:
        if not math.isfinite(close) or close <= 0:
            return
        self.ts.append(float(ts))
        self.open.append(float(open_ if open_ > 0 else close))
        self.high.append(float(high if high > 0 else close))
        self.low.append(float(low if low > 0 else close))
        self.close.append(float(close))
        self.volume.append(float(volume) if math.isfinite(volume) else 0.0)
        self.funding.append(float(funding_pct) if funding_pct is not None and math.isfinite(funding_pct) else float("nan"))
        self.basis.append(float(basis_pct) if basis_pct is not None and math.isfinite(basis_pct) else float("nan"))


class PulseEngine:
    """
    Память монет + композитный PULSE-score.

    Ключ — тикер (BTC). Кормится барами любого ТФ; в бектесте — 4ч,
    в live — 4ч klines (предпочтительно) или агрегированные миды.
    """

    def __init__(self, config: Optional[PulseConfig] = None, bar_seconds: float = 14400.0) -> None:
        self.cfg = config or PulseConfig()
        self.bar_seconds = bar_seconds  # live-агрегация котировок в бары (4ч по умолчанию)
        self._tapes: dict[str, _Tape] = {}
        self._last_side: dict[str, int] = {}
        self._forming: dict[str, list[float]] = {}  # [bucket, o, h, l, c, vol, fund, basis]

    def tape(self, symbol: str) -> _Tape:
        got = self._tapes.get(symbol)
        if got is None:
            got = _Tape(self.cfg.max_bars)
            self._tapes[symbol] = got
        return got

    def observe_bar(
        self,
        symbol: str,
        ts: float,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 0.0,
        funding_pct: Optional[float] = None,
        basis_pct: Optional[float] = None,
    ) -> PulseSnapshot:
        """Добавить бар и вернуть оценку НА ЭТОМ закрытии (без look-ahead)."""
        self.tape(symbol).add(ts, open_, high, low, close, volume, funding_pct, basis_pct)
        return self.snapshot(symbol)

    def observe_quote(
        self,
        symbol: str,
        ts: float,
        mid: float,
        funding_pct: Optional[float] = None,
        basis_pct: Optional[float] = None,
        volume: float = 0.0,
    ) -> PulseSnapshot:
        """
        Живая котировка: агрегируется в бар длительности bar_seconds.
        Закрытый бар пишется в ленту; текущий (ещё открытый) в оценку не входит.
        """
        if not math.isfinite(mid) or mid <= 0 or self.bar_seconds <= 0:
            return self.snapshot(symbol)
        bucket = math.floor(ts / self.bar_seconds) * self.bar_seconds
        forming = self._forming.get(symbol)
        if forming is None:
            self._forming[symbol] = [bucket, mid, mid, mid, mid, volume, funding_pct or float("nan"), basis_pct or float("nan")]
            return self.snapshot(symbol)
        if bucket < forming[0]:
            return self.snapshot(symbol)
        if bucket > forming[0]:
            self.tape(symbol).add(
                forming[0], forming[1], forming[2], forming[3], forming[4],
                forming[5],
                forming[6] if math.isfinite(forming[6]) else None,
                forming[7] if math.isfinite(forming[7]) else None,
            )
            self._forming[symbol] = [bucket, mid, mid, mid, mid, volume, funding_pct or float("nan"), basis_pct or float("nan")]
            return self.snapshot(symbol)
        forming[2] = max(forming[2], mid)
        forming[3] = min(forming[3], mid)
        forming[4] = mid
        forming[5] += volume
        if funding_pct is not None:
            forming[6] = funding_pct
        if basis_pct is not None:
            forming[7] = basis_pct
        return self.snapshot(symbol)

    def snapshot(self, symbol: str) -> PulseSnapshot:
        cfg = self.cfg
        tape = self._tapes.get(symbol)
        if tape is None or not tape.close:
            return PulseSnapshot(symbol=symbol, reasons=["нет данных"])
        closes = list(tape.close)
        highs = list(tape.high)
        lows = list(tape.low)
        vols = list(tape.volume)
        funds = [x for x in tape.funding if math.isfinite(x)]
        bases = [x for x in tape.basis if math.isfinite(x)]
        n = len(closes)
        snap = PulseSnapshot(symbol=symbol, n_bars=n, close=closes[-1])
        if n < cfg.min_bars:
            snap.reasons.append(f"мало баров ({n}/{cfg.min_bars})")
            return snap

        snap.rsi = rsi_last(closes, cfg.rsi_n)
        snap.adx = adx_last(highs, lows, closes, cfg.adx_n)
        atr = atr_last(highs, lows, closes, cfg.atr_n)
        snap.atr_pct = (atr / closes[-1] * 100.0) if atr and closes[-1] > 0 else None
        snap.ema_fast = ema_last(closes, cfg.ema_fast)
        snap.ema_slow = ema_last(closes, cfg.ema_slow)
        snap.macd_hist = macd_last(closes, cfg.ema_fast, cfg.ema_slow, 9)
        snap.bb_pct = bollinger_last(closes, cfg.bb_n, cfg.bb_k)

        if n > cfg.mom_fast:
            snap.mom_fast = closes[-1] / closes[-1 - cfg.mom_fast] - 1.0
        if n > cfg.mom_slow:
            snap.mom_slow = closes[-1] / closes[-1 - cfg.mom_slow] - 1.0
        snap.ret_1 = closes[-1] / closes[-2] - 1.0 if n >= 2 else None

        # σ доходностей (для нормировки моментума)
        rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(max(1, n - 60), n) if closes[i - 1] > 0]
        vol = _std(rets)
        if snap.mom_slow is not None and vol > 0:
            snap.vol_norm_mom = snap.mom_slow / (vol * math.sqrt(cfg.mom_slow))
        else:
            snap.vol_norm_mom = 0.0

        if funds:
            snap.funding_pct = funds[-1]
            snap.funding_z = zscore_last(funds, cfg.funding_z_n) or 0.0
        if bases:
            snap.basis_pct = bases[-1]
            snap.basis_z = zscore_last(bases, cfg.basis_z_n) or 0.0
        if any(v > 0 for v in vols):
            snap.volume_z = zscore_last(vols, 40) or 0.0

        # --- режим ----------------------------------------------------------
        if snap.adx is not None and snap.adx >= cfg.adx_trend:
            snap.regime = "TREND"
        else:
            snap.regime = "RANGE"

        # компоненты −1…+1
        mom = tanh_clip(snap.vol_norm_mom or 0.0, 1.2)
        crowd = -tanh_clip(snap.funding_z or 0.0, 1.4)   # fade crowding
        basis = -tanh_clip(snap.basis_z or 0.0, 1.4)
        vol_c = tanh_clip(snap.volume_z or 0.0, 1.5)
        # короткий mean-reversion: сильный бар против нас
        mr = 0.0
        if snap.ret_1 is not None and vol > 0:
            mr = -tanh_clip(snap.ret_1 / max(vol, 1e-9), 1.5)
        rsi_c = 0.0
        if snap.rsi is not None:
            # работает только на экстремумах; в середине ≈ 0
            if snap.rsi <= 35:
                rsi_c = (35.0 - snap.rsi) / 35.0
            elif snap.rsi >= 65:
                rsi_c = (65.0 - snap.rsi) / 35.0

        # volume подтверждает направление моментума, не задаёт его
        vol_confirm = vol_c * (1.0 if mom >= 0 else -1.0)

        if snap.regime == "TREND":
            score = (
                cfg.w_mom * mom
                + cfg.w_crowd_trend * crowd
                + cfg.w_basis_trend * basis
                + cfg.w_vol_trend * vol_confirm
            )
        else:
            score = (
                cfg.w_mr * mr
                + cfg.w_rsi * rsi_c
                + cfg.w_crowd_range * crowd
                + cfg.w_basis_range * basis
            )

        # жёсткий override: экстремальный crowding важнее тренда
        fz = snap.funding_z or 0.0
        if abs(fz) >= cfg.crowding_extreme:
            score = 0.65 * crowd + 0.35 * score
            snap.reasons.append(f"экстремальный crowding z={fz:+.2f}")

        score = max(-1.0, min(1.0, score))
        snap.score = score

        prev = self._last_side.get(symbol, 0)
        if prev == 0:
            side = 1 if score >= cfg.score_entry else (-1 if score <= -cfg.score_entry else 0)
        elif prev > 0:
            side = 0 if score <= cfg.score_exit else 1
            if score <= -cfg.score_entry:
                side = -1
        else:
            side = 0 if score >= -cfg.score_exit else -1
            if score >= cfg.score_entry:
                side = 1
        self._last_side[symbol] = side
        snap.side = side

        conf = 40.0
        conf += min(30.0, abs(score) * 50.0)
        if snap.adx is not None:
            conf += min(10.0, max(0.0, snap.adx - 15.0) / 2.0)
        if abs(fz) >= 1.2:
            conf += 8.0
        if n >= cfg.min_bars * 2:
            conf += 7.0
        snap.confidence = int(max(0, min(100, round(conf))))
        snap.grade = (
            "A" if snap.confidence >= 80 and abs(score) >= 0.55
            else "B" if snap.confidence >= 65 and abs(score) >= cfg.score_entry
            else "C" if abs(score) >= cfg.score_exit
            else "D"
        )
        snap.reasons.append(
            f"{snap.regime}: mom={mom:+.2f} crowd={crowd:+.2f} mr={mr:+.2f} rsi={rsi_c:+.2f}"
        )
        return snap

    def rank(self, limit: int = 10, *, actionable_only: bool = False) -> list[PulseSnapshot]:
        """Монеты, отсортированные по |score| (самые «уверенные» направления)."""
        rows = [self.snapshot(sym) for sym in self._tapes]
        if actionable_only:
            rows = [r for r in rows if r.actionable]
        rows.sort(key=lambda s: (abs(s.score), s.confidence), reverse=True)
        return rows[:limit]
