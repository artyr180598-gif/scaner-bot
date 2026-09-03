"""
Хранилище сигналов (aiosqlite + минимальная схема).

Идея: каждая публикация сигнала пишется в БД. Это нужно для:
- трекинга исходов (hit TP/SL),
- аудита,
- калибровки уверенности (на будущее).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from ...core.domain.signal import Signal, SignalStatus

log = logging.getLogger(__name__)


class SignalStore:
    """Асинхронная обёртка над SQLite."""

    def __init__(self, path: str = "signals.db") -> None:
        self.path = path
        self._db: Any = None  # aiosqlite connection

    async def start(self) -> None:
        try:
            import aiosqlite  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("aiosqlite required: pip install aiosqlite") from exc
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT UNIQUE NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence_data REAL NOT NULL,
                confidence_signal REAL NOT NULL,
                entry_low REAL, entry_high REAL, entry_mid REAL,
                stop_loss REAL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                risk_reward REAL,
                leverage REAL,
                entry_logic TEXT,
                logic_factors TEXT,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                published_at REAL,
                closed_at REAL,
                pnl_pct REAL
            )
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def save(self, signal: Signal) -> None:
        assert self._db is not None, "store not started"
        plan = signal.plan
        sl = plan.stop_loss.price if plan.stop_loss else None
        tps = [tp.price for tp in plan.take_profits] + [None] * 3
        await self._db.execute(
            """
            INSERT OR REPLACE INTO signals (
                signal_id, exchange, symbol, direction,
                confidence_data, confidence_signal,
                entry_low, entry_high, entry_mid,
                stop_loss, tp1, tp2, tp3,
                risk_reward, leverage,
                entry_logic, logic_factors,
                status, created_at, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.signal_id,
                signal.exchange,
                signal.symbol,
                signal.direction.value,
                signal.confidences.data,
                signal.confidences.signal,
                plan.entry_zone[0],
                plan.entry_zone[1],
                plan.entry_mid,
                sl,
                tps[0], tps[1], tps[2],
                plan.risk_reward,
                plan.leverage_suggestion,
                signal.entry_logic,
                json.dumps(signal.logic_factors, ensure_ascii=False),
                signal.status.value,
                signal.created_at or time.time(),
                time.time() if signal.status == SignalStatus.PUBLISHED else None,
            ),
        )
        await self._db.commit()

    async def update_status(
        self,
        signal_id: str,
        status: SignalStatus,
        pnl_pct: Optional[float] = None,
    ) -> None:
        assert self._db is not None, "store not started"
        await self._db.execute(
            "UPDATE signals SET status=?, closed_at=?, pnl_pct=? WHERE signal_id=?",
            (status.value, time.time(), pnl_pct, signal_id),
        )
        await self._db.commit()

    async def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        assert self._db is not None, "store not started"
        cur = await self._db.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        await cur.close()
        cols = [d[0] for d in cur.description] if rows else []
        return [dict(zip(cols, r)) for r in rows]
