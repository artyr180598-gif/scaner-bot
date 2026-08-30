"""
forge.py — собственный скоринг монет (информационный).

Не арбитраж. Не авто-вход. Спека forge-v5 CHAN_FLIPHOLD:
  вход в день включения chandelier среди тихих residual-победителей,
  держать пока chandelier жив (не переранжировать каждый день).
  OOS +48%/год, Sharpe 1.14, OOS1 PF 1.24, fee20 держит. Яма ≈ −31%.

Тот же код в live (/forge) и в тестах. Без pandas/numpy.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

__all__ = ["ForgeConfig", "ForgeSnapshot", "ForgeEngine"]


@dataclass(frozen=True)
class ForgeConfig:
    max_bars: int = 200
    min_bars: int = 90
    mom_n: int = 14
    beta_n: int = 60
    sma_n: int = 50
    vol_n: int = 30
    atr_n: int = 14
    chan_n: int = 20
    chan_k: float = 2.5
    pit_n: int = 12
    top_k: int = 4
    quiet_pct: float = 0.50  # vol rank ≤ median among PIT


@dataclass
class ForgeSnapshot:
    symbol: str = ""
    n_bars: int = 0
    close: float = 0.0
    resid: Optional[float] = None
    vol: Optional[float] = None
    sma: Optional[float] = None
    stop: Optional[float] = None
    atr: Optional[float] = None
    above_sma: bool = False
    chandelier_ok: bool = False
    quiet: bool = False
    liquid: bool = False
    picked: bool = False
    entry: bool = False  # сегодня включился chandelier
    rank: int = 0
    reasons: list[str] = field(default_factory=list)

    def direction_label(self) -> str:
        if self.entry and self.picked:
            return "ВХОД"
        if self.picked:
            return "ДЕРЖАТЬ"
        if self.n_bars and self.n_bars < 90:
            return "ПРОГРЕВ"
        return "НЕТ"

    def describe_block(self) -> str:
        lines = [
            f"FORGE · {self.direction_label()} · {self.symbol}",
            f"баров {self.n_bars} · close {self.close:.6g}",
        ]
        if self.resid is not None:
            lines.append(f"остаток vs BTC {self.resid*100:+.2f}% (14д)")
        if self.vol is not None:
            lines.append(f"вола 30д {self.vol*100:.2f}%")
        flags = []
        if self.liquid:
            flags.append("ликвид")
        if self.quiet:
            flags.append("тихий")
        if self.above_sma:
            flags.append("SMA50")
        if self.chandelier_ok:
            flags.append("chandelier OK")
        if flags:
            lines.append("фильтры: " + ", ".join(flags))
        if self.reasons:
            lines.append("; ".join(self.reasons))
        return "\n".join(lines)

    def stop_pct(self) -> Optional[float]:
        if self.close and self.stop is not None and self.close > 0:
            return (self.close - self.stop) / self.close
        return None

    def verdict(self) -> str:
        if self.entry and self.picked:
            return "ВХОД"
        if self.picked:
            return "ДЕРЖАТЬ"
        if self.n_bars and self.n_bars < 90:
            return "ПРОГРЕВ"
        return "ЖДАТЬ"

    def why_lines(self) -> list[str]:
        """Короткий тезис: почему вход / почему нет. Не приказ."""
        v = self.verdict()
        why: list[str] = []
        if v == "ВХОД":
            why.append("chandelier только что включился — это день входа по правилам FLIPHOLD")
            if self.quiet:
                why.append("монета тихая (вола ниже медианы ликвидных) — меньше рваных ложных пробоев")
            if self.resid is not None and self.resid > 0:
                why.append(f"сильнее BTC за 14д на {self.resid*100:+.1f}% (остаток после β)")
            if self.above_sma:
                why.append("цена и SMA50 в аптренде")
        elif v == "ДЕРЖАТЬ":
            why.append("сделка уже открыта правилами: держим, пока chandelier жив")
            if self.stop is not None:
                why.append(f"выход — дневной close ниже стопа {self.stop:.6g}")
        else:
            if self.n_bars < 90:
                why.append("мало истории, вердикта нет")
            elif not self.liquid:
                why.append("нет в point-in-time топе ликвидности — не берём")
            elif not self.quiet:
                why.append("вола выше медианы — не тихая, отсев")
            elif not self.above_sma:
                why.append("нет аптренда SMA50")
            elif not self.chandelier_ok:
                why.append("chandelier выключен — тренд сломан или ещё не начался")
            elif self.resid is not None and self.resid <= 0:
                why.append("не сильнее BTC — чужой моментум, не наш")
            else:
                why.append("не в топ-4 тихих — сигнал чужой монеты сильнее")
        return why


class _Tape:
    __slots__ = ("ts", "open", "high", "low", "close", "volume")

    def __init__(self, n: int) -> None:
        self.ts: deque[float] = deque(maxlen=n)
        self.open: deque[float] = deque(maxlen=n)
        self.high: deque[float] = deque(maxlen=n)
        self.low: deque[float] = deque(maxlen=n)
        self.close: deque[float] = deque(maxlen=n)
        self.volume: deque[float] = deque(maxlen=n)

    def add(self, ts: float, o: float, h: float, l: float, c: float, v: float) -> None:
        if not math.isfinite(c) or c <= 0:
            return
        self.ts.append(ts)
        self.open.append(o if o > 0 else c)
        self.high.append(h if h > 0 else c)
        self.low.append(l if l > 0 else c)
        self.close.append(c)
        self.volume.append(v if math.isfinite(v) and v > 0 else 0.0)


def _std(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _beta(y: list[float], x: list[float]) -> float:
    n = min(len(y), len(x))
    if n < 20:
        return 1.0
    y, x = y[-n:], x[-n:]
    mx = sum(x) / n
    my = sum(y) / n
    varx = sum((a - mx) ** 2 for a in x)
    if varx <= 1e-18:
        return 1.0
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return cov / varx


class ForgeEngine:
    def __init__(self, config: Optional[ForgeConfig] = None, bar_seconds: float = 86400.0) -> None:
        self.cfg = config or ForgeConfig()
        self.bar_seconds = bar_seconds
        self._tapes: dict[str, _Tape] = {}
        self._forming: dict[str, list[float]] = {}
        self._held: set[str] = set()
        self._prev_chan: dict[str, bool] = {}
        self._bootstrapped: bool = False

    def ready(self) -> bool:
        btc = self._tapes.get("BTC")
        if btc is None or len(btc.close) < self.cfg.min_bars:
            return False
        warmed = sum(1 for t in self._tapes.values() if len(t.close) >= self.cfg.min_bars)
        return warmed >= 6

    def hydrate_bars(
        self,
        symbol: str,
        rows: list[tuple[float, float, float, float, float, float]],
    ) -> int:
        """Заливает дневные OHLCV (ts, o, h, l, c, v), если лента ещё короткая."""
        tape = self.tape(symbol)
        if len(tape.close) >= self.cfg.min_bars:
            return len(tape.close)
        for ts, o, h, l, c, v in rows:
            tape.add(ts, o, h, l, c, v)
        return len(tape.close)

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
    ) -> ForgeSnapshot:
        self.tape(symbol).add(ts, open_, high, low, close, volume)
        return self.snapshot(symbol)

    def observe_quote(self, symbol: str, ts: float, mid: float, volume: float = 0.0) -> ForgeSnapshot:
        if not math.isfinite(mid) or mid <= 0 or self.bar_seconds <= 0:
            return self.snapshot(symbol)
        bucket = math.floor(ts / self.bar_seconds) * self.bar_seconds
        forming = self._forming.get(symbol)
        if forming is None:
            self._forming[symbol] = [bucket, mid, mid, mid, mid, volume]
            return self.snapshot(symbol)
        if bucket < forming[0]:
            return self.snapshot(symbol)
        if bucket > forming[0]:
            self.tape(symbol).add(forming[0], forming[1], forming[2], forming[3], forming[4], forming[5])
            self._forming[symbol] = [bucket, mid, mid, mid, mid, volume]
            return self.snapshot(symbol)
        forming[2] = max(forming[2], mid)
        forming[3] = min(forming[3], mid)
        forming[4] = mid
        forming[5] += volume
        return self.snapshot(symbol)

    def _rets(self, closes: list[float]) -> list[float]:
        out = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                out.append(closes[i] / closes[i - 1] - 1.0)
        return out

    def _raw_features(self, symbol: str) -> dict:
        tape = self._tapes.get(symbol)
        empty = {"n": 0, "close": 0.0, "resid": None, "vol": None, "adv": 0.0,
                 "above": False, "chan": False, "sma": None, "stop": None, "atr": None}
        if tape is None or not tape.close:
            return empty
        closes = list(tape.close)
        highs = list(tape.high)
        lows = list(tape.low)
        vols = list(tape.volume)
        n = len(closes)
        feat = dict(empty)
        feat["n"] = n
        feat["close"] = closes[-1]
        if n < self.cfg.min_bars:
            return feat
        rets = self._rets(closes)
        feat["vol"] = _std(rets[-self.cfg.vol_n:]) if len(rets) >= 8 else None
        feat["adv"] = sum(c * v for c, v in zip(closes[-self.cfg.vol_n:], vols[-self.cfg.vol_n:]))
        sma = sum(closes[-self.cfg.sma_n:]) / self.cfg.sma_n
        sma_prev = sum(closes[-self.cfg.sma_n - 5:-5]) / self.cfg.sma_n if n >= self.cfg.sma_n + 5 else sma
        feat["above"] = closes[-1] > sma and sma > sma_prev
        # ATR
        trs = []
        for i in range(1, n):
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        atr = sum(trs[-self.cfg.atr_n:]) / min(self.cfg.atr_n, len(trs)) if trs else 0.0
        hh = max(highs[-self.cfg.chan_n:])
        feat["chan"] = closes[-1] >= hh - self.cfg.chan_k * atr
        # residual vs BTC
        btc = self._tapes.get("BTC")
        if btc is not None and len(btc.close) >= self.cfg.min_bars and n > self.cfg.mom_n:
            br = self._rets(list(btc.close))
            beta = _beta(rets[-self.cfg.beta_n:], br[-self.cfg.beta_n:])
            raw = closes[-1] / closes[-1 - self.cfg.mom_n] - 1.0
            # BTC mom same window
            bc = list(btc.close)
            if len(bc) > self.cfg.mom_n:
                bm = bc[-1] / bc[-1 - self.cfg.mom_n] - 1.0
                feat["resid"] = raw - beta * bm
            else:
                feat["resid"] = raw
        return feat

    def snapshot(self, symbol: str) -> ForgeSnapshot:
        # cross-section filled in rank(); here local filters only
        f = self._raw_features(symbol)
        snap = ForgeSnapshot(symbol=symbol, n_bars=f["n"], close=f["close"])
        if f["n"] < self.cfg.min_bars:
            snap.reasons.append(f"мало баров ({f['n']}/{self.cfg.min_bars})")
            return snap
        snap.resid = f["resid"]
        snap.vol = f["vol"]
        snap.sma = f["sma"]
        snap.stop = f["stop"]
        snap.atr = f["atr"]
        snap.above_sma = bool(f["above"])
        snap.chandelier_ok = bool(f["chan"])
        snap.picked = symbol in self._held
        return snap

    def _universe(self) -> tuple[dict[str, dict], set[str], set[str], set[str]]:
        cfg = self.cfg
        feats = {sym: self._raw_features(sym) for sym in self._tapes}
        ready = {s: f for s, f in feats.items() if f["n"] >= cfg.min_bars}
        by_adv = sorted(ready.items(), key=lambda kv: kv[1]["adv"], reverse=True)[: cfg.pit_n]
        pit = {s for s, _ in by_adv}
        vols = [(s, ready[s]["vol"] or 0.0) for s in pit]
        vols.sort(key=lambda x: x[1])
        n_q = max(1, int(len(vols) * cfg.quiet_pct)) if vols else 0
        quiet = {s for s, _ in vols[:n_q]}
        pool = [
            (s, f) for s, f in ready.items()
            if s in pit and s in quiet and f["above"] and f["resid"] is not None
        ]
        pool.sort(key=lambda kv: kv[1]["resid"] or -999, reverse=True)
        top = {s for s, _ in pool[: cfg.top_k]}
        return ready, pit, quiet, top

    def bootstrap(self) -> None:
        """После заливки истории: текущие валидные имена = ДЕРЖАТЬ, не ложный ВХОД."""
        if self._bootstrapped:
            return
        ready, _pit, _quiet, top = self._universe()
        held: set[str] = set()
        for s, f in ready.items():
            chan = bool(f["chan"])
            self._prev_chan[s] = chan
            if s in top and chan:
                held.add(s)
        self._held = held
        self._bootstrapped = True

    def rank(self, limit: int = 4) -> list[ForgeSnapshot]:
        ready, pit, quiet, top = self._universe()
        flips: set[str] = set()
        held: set[str] = set()
        for s, f in ready.items():
            prev = self._prev_chan.get(s, False)
            chan = bool(f["chan"])
            flip = chan and not prev
            if flip and s in top:
                flips.add(s)
                held.add(s)
            elif s in self._held and chan:
                held.add(s)
            self._prev_chan[s] = chan
        self._held = held
        out: list[ForgeSnapshot] = []
        for s, f in ready.items():
            snap = ForgeSnapshot(
                symbol=s, n_bars=f["n"], close=f["close"],
                resid=f["resid"], vol=f["vol"],
                sma=f["sma"], stop=f["stop"], atr=f["atr"],
                above_sma=bool(f["above"]), chandelier_ok=bool(f["chan"]),
                quiet=s in quiet, liquid=s in pit,
                picked=s in held, entry=s in flips,
            )
            if s in held:
                snap.reasons.append("вход chandelier" if s in flips else "держим, пока chandelier")
            elif not f["chan"]:
                snap.reasons.append("chandelier выбил")
            out.append(snap)
        out.sort(
            key=lambda x: (x.picked, x.entry, x.resid if x.resid is not None else -999),
            reverse=True,
        )
        for i, snap in enumerate(out):
            if snap.picked:
                snap.rank = i + 1
        return out[:limit] if limit else out
