"""
signal_history.py — честный трекинг точности выданных сигналов.

Идея простая и намеренно «неудобная» для бота: как только сигнал показан
пользователю как ACTIONABLE, он записывается в журнал вместе с планом
(вход/стоп/цели). Дальше при каждом появлении свежей цены по этой монете
запись обновляется, и результат фиксируется по факту:

    tp1 / tp2 / tp3 — цена дошла до цели раньше, чем до стопа
    stop            — цена сходила на стоп
    expired         — истёк срок жизни идеи (по умолчанию 24 ч), считаем
                      по последней цене (плюс/минус — как есть)

Win-rate показывается как есть, даже если он плохой. Никаких «ретроспективных
подкруток» (изменить стоп задним числом) в API нет — только `record` и
`update_price`.

Хранилище — обычный JSON-файл (Railway worker переживает рестарты только с
volume; без него журнал начинается заново — это честно отражается в /accuracy
как «наблюдений: N»).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

log = logging.getLogger("history")

__all__ = ["SignalRecord", "SignalHistory"]

OPEN = "open"
TERMINAL = ("tp1", "tp2", "tp3", "stop", "expired")


@dataclass
class SignalRecord:
    """Один выданный сигнал + его судьба."""

    id: str
    base: str
    exchange: str
    direction: str
    created_at: float
    entry_low: float
    entry_high: float
    stop: float
    targets: list[float]
    rr: float
    data_confidence: float
    signal_confidence: float
    profile: str
    status: str = OPEN
    max_favorable_percent: float = 0.0
    max_adverse_percent: float = 0.0
    result_percent: Optional[float] = None
    closed_at: Optional[float] = None
    last_price: Optional[float] = None

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def is_open(self) -> bool:
        return self.status == OPEN

    @property
    def is_win(self) -> Optional[bool]:
        if self.status in ("tp1", "tp2", "tp3"):
            return True
        if self.status == "stop":
            return False
        if self.status == "expired" and self.result_percent is not None:
            return self.result_percent > 0
        return None


class SignalHistory:
    """Журнал сигналов с подсчётом фактической точности."""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        max_records: int = 2000,
        ttl_hours: float = 24.0,
    ) -> None:
        self.path = path
        self.max_records = max_records
        self.ttl_seconds = ttl_hours * 3600.0
        self.records: list[SignalRecord] = []
        self._load()

    # ------------------------------------------------------------- хранилище
    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self.records = [SignalRecord(**item) for item in raw]
            log.info("Журнал сигналов загружен: %d записей", len(self.records))
        except Exception as exc:  # noqa: BLE001 — битый файл не должен ронять бота
            log.warning("Не смог прочитать журнал сигналов (%s): %s", self.path, exc)
            self.records = []

    def save(self) -> None:
        if not self.path:
            return
        try:
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", dir=directory, delete=False, encoding="utf-8"
            ) as tmp:
                json.dump([asdict(r) for r in self.records], tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не смог сохранить журнал сигналов: %s", exc)

    # ------------------------------------------------------------------ запись
    def record(self, signal: Any, *, dedupe_minutes: float = 30.0) -> Optional[SignalRecord]:
        """
        Записывает ACTIONABLE-сигнал. Повторный такой же сигнал по монете в
        течение `dedupe_minutes` игнорируется (иначе win-rate накручивается
        десятками копий одной идеи).
        """
        plan = getattr(signal, "plan", None)
        if not getattr(signal, "actionable", False) or plan is None:
            return None
        now = float(getattr(signal, "generated_at", time.time()))
        for rec in reversed(self.records):
            if (
                rec.base == signal.base
                and rec.direction == plan.direction
                and rec.is_open
                and now - rec.created_at < dedupe_minutes * 60.0
            ):
                return None
        rec = SignalRecord(
            id=f"{signal.base}-{int(now)}",
            base=signal.base,
            exchange=signal.exchange,
            direction=plan.direction,
            created_at=now,
            entry_low=plan.entry_low,
            entry_high=plan.entry_high,
            stop=plan.stop,
            targets=list(plan.targets),
            rr=plan.rr,
            data_confidence=signal.data_confidence,
            signal_confidence=signal.signal_confidence,
            profile=signal.profile.key,
            last_price=signal.price,
        )
        self.records.append(rec)
        if len(self.records) > self.max_records:
            self.records = self.records[-self.max_records:]
        self.save()
        return rec

    # ---------------------------------------------------------------- апдейты
    def update_price(self, base: str, price: float, now: Optional[float] = None) -> int:
        """
        Двигает все открытые записи по монете. Возвращает число закрытых.

        Консервативное допущение: если в одном обновлении цена могла задеть и
        стоп, и цель — считаем СТОП (пессимистично). Реальный тик-уровень нам
        недоступен, и завышать точность нельзя.
        """
        now = now or time.time()
        closed = 0
        for rec in self.records:
            if not rec.is_open or rec.base != base:
                continue
            rec.last_price = price
            sign = 1.0 if rec.direction == "long" else -1.0
            move_pct = (price - rec.entry_mid) / rec.entry_mid * 100.0 * sign
            rec.max_favorable_percent = max(rec.max_favorable_percent, move_pct)
            rec.max_adverse_percent = min(rec.max_adverse_percent, move_pct)

            hit_stop = price <= rec.stop if rec.direction == "long" else price >= rec.stop
            if hit_stop:
                rec.status = "stop"
                rec.result_percent = (rec.stop - rec.entry_mid) / rec.entry_mid * 100.0 * sign
                rec.closed_at = now
                closed += 1
                continue
            reached = 0
            for i, tgt in enumerate(rec.targets, start=1):
                hit = price >= tgt if rec.direction == "long" else price <= tgt
                if hit:
                    reached = i
            if reached:
                rec.status = f"tp{min(reached, 3)}"
                tgt = rec.targets[min(reached, len(rec.targets)) - 1]
                rec.result_percent = (tgt - rec.entry_mid) / rec.entry_mid * 100.0 * sign
                rec.closed_at = now
                closed += 1
                continue
            if now - rec.created_at > self.ttl_seconds:
                rec.status = "expired"
                rec.result_percent = move_pct
                rec.closed_at = now
                closed += 1
        if closed:
            self.save()
        return closed

    # ------------------------------------------------------------- статистика
    def stats(self, base: Optional[str] = None) -> dict[str, Any]:
        rows = [r for r in self.records if base is None or r.base == base.upper()]
        closed = [r for r in rows if not r.is_open]
        wins = [r for r in closed if r.is_win is True]
        losses = [r for r in closed if r.is_win is False]
        results = [r.result_percent for r in closed if r.result_percent is not None]
        gross_win = sum(x for x in results if x > 0)
        gross_loss = -sum(x for x in results if x < 0)
        return {
            "total": len(rows),
            "open": sum(1 for r in rows if r.is_open),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": (len(wins) / len(closed) * 100.0) if closed else None,
            "avg_result_percent": (sum(results) / len(results)) if results else None,
            "sum_result_percent": sum(results) if results else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
            "by_status": {
                status: sum(1 for r in closed if r.status == status)
                for status in TERMINAL
            },
            "avg_rr": (sum(r.rr for r in rows) / len(rows)) if rows else None,
            "avg_signal_confidence": (
                sum(r.signal_confidence for r in rows) / len(rows) if rows else None
            ),
        }

    def recent(self, limit: int = 10, base: Optional[str] = None) -> list[SignalRecord]:
        rows = [r for r in self.records if base is None or r.base == base.upper()]
        return list(reversed(rows[-limit:]))

    def open_bases(self) -> list[str]:
        return sorted({r.base for r in self.records if r.is_open})
