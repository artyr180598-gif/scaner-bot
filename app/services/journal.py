"""
app/services/journal.py — журнал сигналов и честный учёт их исходов.

Зачем: «уверенность» в таких системах легко превращается в самообман. Журнал
пишет каждый опубликованный сигнал, а потом по реальным свечам определяет, что
случилось раньше — стоп или цели. По накопленной статистике бот честно
показывает точность по корзинам уверенности (команда /stats), и если связь
«уверенность → результат» отсутствует, это видно сразу.

Хранение — JSON-файл (без внешних зависимостей и без миграций).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.domain.models import Direction, Signal

log = logging.getLogger(__name__)

OUTCOME_OPEN = "open"
OUTCOME_TP1 = "TP1"
OUTCOME_TP2 = "TP2"
OUTCOME_TP3 = "TP3"
OUTCOME_STOP = "STOP"
OUTCOME_EXPIRED = "EXPIRED"
OUTCOME_INVALIDATED = "INVALIDATED"


@dataclass(slots=True)
class JournalEntry:
    symbol: str
    direction: str
    confidence: float
    potential: float
    timeframe: str
    created_at: str
    entry_low: float
    entry_high: float
    stop: float
    targets: List[float]
    rr: float
    setup: str
    price: float
    horizon_hours: int = 24
    outcome: str = OUTCOME_OPEN
    outcome_at: Optional[str] = None
    outcome_price: float = float("nan")
    r_result: float = 0.0            # результат в R (−1 = стоп, +1.5 = TP1.5R)
    max_favourable_r: float = 0.0
    max_adverse_r: float = 0.0
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        for key, value in list(out.items()):
            if isinstance(value, float) and not math.isfinite(value):
                out[key] = None
        return out

    @classmethod
    def from_signal(cls, signal: Signal, potential: float = 0.0) -> "JournalEntry":
        plan = signal.plan
        return cls(
            symbol=signal.symbol,
            direction=signal.direction.value,
            confidence=signal.confidence,
            potential=potential,
            timeframe=signal.timeframe.value,
            created_at=signal.created_at.isoformat(),
            entry_low=plan.entry_low if plan else signal.price,
            entry_high=plan.entry_high if plan else signal.price,
            stop=plan.stop if plan else float("nan"),
            targets=[t.price for t in plan.targets] if plan else [],
            rr=round(signal.rr, 2),
            setup=signal.setup,
            price=signal.price,
            horizon_hours=signal.horizon_hours,
        )


class SignalJournal:
    """Журнал сигналов с отслеживанием исходов по реальным свечам."""

    def __init__(self, path: Path, max_entries: int = 2000) -> None:
        self.path = Path(path)
        self.max_entries = int(max_entries)
        self._entries: List[JournalEntry] = []
        self._lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Хранение
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("entries", []):
                self._entries.append(JournalEntry(**_clean(item, JournalEntry)))
            log.info("журнал: загружено %d записей из %s", len(self._entries), self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("журнал повреждён (%s) — начинаю с чистого", exc)
            self._entries = []

    async def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "entries": [e.to_dict() for e in self._entries[-self.max_entries:]],
        }
        # Атомарная запись: не убиваем файл при падении процесса.
        with tempfile.NamedTemporaryFile("w", dir=str(self.path.parent),
                                         delete=False, suffix=".tmp",
                                         encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            tmp = Path(fh.name)
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    async def add(self, signal: Signal, potential: float = 0.0) -> None:
        async with self._lock:
            self._entries.append(JournalEntry.from_signal(signal, potential))
            self._entries = self._entries[-self.max_entries:]
            await self._flush()

    def entries(self, limit: int = 20, symbol: Optional[str] = None
                ) -> List[JournalEntry]:
        items = [e for e in self._entries if not symbol or e.symbol == symbol]
        return list(reversed(items[-limit:]))

    def open_entries(self) -> List[JournalEntry]:
        return [e for e in self._entries if e.outcome == OUTCOME_OPEN]

    def is_recent(self, symbol: str, direction: str,
                  cooldown_minutes: int = 120) -> bool:
        """Не дублируем один и тот же сигнал в течение кулдауна."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
        for entry in reversed(self._entries):
            if entry.symbol != symbol or entry.direction != direction:
                continue
            try:
                created = datetime.fromisoformat(entry.created_at)
            except ValueError:
                continue
            if created >= cutoff:
                return True
            break
        return False

    # ------------------------------------------------------------------
    # Отслеживание исходов
    # ------------------------------------------------------------------
    async def update_outcomes(self, market, timeframe_hint: Optional[str] = None
                              ) -> int:
        """
        Проверяет открытые сигналы по реальным свечам и закрывает их.

        Правила честности (те же, что в бектесте):
          * если в одном баре задеты и стоп, и цель — засчитывается СТОП
            (внутрибарный порядок нам неизвестен);
          * цель считается достигнутой, только если цена реально дошла до неё;
          * сигнал «протух», если горизонт прошёл, а ничего не случилось.
        """
        updated = 0
        for entry in self.open_entries():
            try:
                if await self._resolve(entry, market):
                    updated += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: не удалось проверить исход: %s", entry.symbol, exc)
        if updated:
            async with self._lock:
                await self._flush()
        return updated

    async def _resolve(self, entry: JournalEntry, market) -> bool:
        from app.domain.models import Timeframe

        created = datetime.fromisoformat(entry.created_at)
        tf = Timeframe.parse(entry.timeframe)
        candles = await market.candles(entry.symbol, tf, 300)
        if candles is None or candles.empty:
            return False
        df = candles.df[candles.df.index >= created]
        if len(df) == 0:
            return False

        entry_mid = (entry.entry_low + entry.entry_high) / 2
        risk = abs(entry_mid - entry.stop)
        if risk <= 0:
            return False
        sign = 1 if entry.direction == Direction.LONG.value else -1

        best_r = 0.0
        worst_r = 0.0
        outcome: Optional[str] = None
        outcome_price = float("nan")

        for _, row in df.iterrows():
            high, low = float(row["high"]), float(row["low"])
            fav = ((high - entry_mid) if sign > 0 else (entry_mid - low)) / risk
            adv = ((entry_mid - low) if sign > 0 else (high - entry_mid)) / risk
            best_r = max(best_r, fav)
            worst_r = max(worst_r, adv)

            stop_hit = (low <= entry.stop) if sign > 0 else (high >= entry.stop)
            targets_hit = [
                i for i, tp in enumerate(entry.targets)
                if (high >= tp if sign > 0 else low <= tp)
            ]
            if stop_hit:
                # Пессимизм: стоп важнее цели в том же баре.
                outcome, outcome_price = OUTCOME_STOP, entry.stop
                break
            if targets_hit:
                idx_hit = max(targets_hit)
                outcome = (OUTCOME_TP3 if idx_hit >= 2 else
                           OUTCOME_TP2 if idx_hit == 1 else OUTCOME_TP1)
                outcome_price = entry.targets[idx_hit]
                break

        horizon = timedelta(hours=max(6, int(entry.horizon_hours or 24)))
        expired = datetime.now(timezone.utc) - created > horizon
        if outcome is None and expired:
            outcome, outcome_price = OUTCOME_EXPIRED, candles.last_price

        if outcome is None:
            entry.max_favourable_r = round(best_r, 2)
            entry.max_adverse_r = round(-worst_r, 2)
            return False

        entry.outcome = outcome
        entry.outcome_at = datetime.now(timezone.utc).isoformat()
        entry.outcome_price = outcome_price
        entry.max_favourable_r = round(best_r, 2)
        entry.max_adverse_r = round(-worst_r, 2)
        entry.r_result = round(
            {OUTCOME_STOP: -1.0, OUTCOME_TP1: 1.0, OUTCOME_TP2: 2.0,
             OUTCOME_TP3: 3.0, OUTCOME_EXPIRED: 0.0}.get(outcome, 0.0), 2)
        return True

    # ------------------------------------------------------------------
    # Статистика
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        closed = [e for e in self._entries if e.outcome != OUTCOME_OPEN]
        if not closed:
            return {"total": len(self._entries), "closed": 0,
                    "note": "статистики пока нет: ни один сигнал ещё не закрылся"}
        tp = [e for e in closed if e.outcome in (OUTCOME_TP1, OUTCOME_TP2, OUTCOME_TP3)]
        stops = [e for e in closed if e.outcome == OUTCOME_STOP]
        r_sum = sum(e.r_result for e in closed)
        buckets = _calibration_buckets(closed)
        return {
            "total": len(self._entries),
            "closed": len(closed),
            "open": len(self._entries) - len(closed),
            "win_rate": round(len(tp) / len(closed) * 100, 1),
            "stop_rate": round(len(stops) / len(closed) * 100, 1),
            "avg_r": round(r_sum / len(closed), 2),
            "total_r": round(r_sum, 1),
            "calibration": buckets,
        }


def _calibration_buckets(entries: List[JournalEntry]) -> List[Dict[str, Any]]:
    """
    Результат по корзинам уверенности — проверка, работает ли «уверенность»
    вообще. Именно так проект узнал, что высокая уверенность НЕ гарантирует
    прибыль (см. AI_AGENTS/BRAIN.md).
    """
    edges = [(0, 5), (5, 6.5), (6.5, 8), (8, 10.1)]
    out: List[Dict[str, Any]] = []
    for lo, hi in edges:
        bucket = [e for e in entries if lo <= e.confidence < hi]
        if not bucket:
            continue
        wins = sum(1 for e in bucket if e.outcome in (OUTCOME_TP1, OUTCOME_TP2, OUTCOME_TP3))
        out.append({
            "range": f"{lo:g}–{hi:g}",
            "n": len(bucket),
            "win_rate": round(wins / len(bucket) * 100, 1),
            "avg_r": round(sum(e.r_result for e in bucket) / len(bucket), 2),
        })
    return out


def _clean(item: Dict[str, Any], cls) -> Dict[str, Any]:
    """Отсекает лишние ключи (совместимость со старыми файлами журнала)."""
    allowed = {f for f in cls.__slots__}
    cleaned = {k: v for k, v in item.items() if k in allowed}
    for key, value in list(cleaned.items()):
        if value is None and key in ("outcome_price", "r_result",
                                     "max_favourable_r", "max_adverse_r"):
            cleaned[key] = float("nan") if key == "outcome_price" else 0.0
    return cleaned
