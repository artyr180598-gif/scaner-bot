"""Хранилище сигналов в SQLite (aiosqlite) — журнал советов."""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Optional

from ..core.domain.signal import Signal

log = logging.getLogger(__name__)


class SignalStore:
    """Лёгкий SQLite-журнал выданных сигналов."""

    def __init__(self, path: str = "signals.db") -> None:
        self.path = path
        self._db = None

    async def start(self) -> None:
        try:
            import aiosqlite
        except ImportError as exc:  # pragma: no cover
            log.warning("aiosqlite not installed — сигналы не сохраняются: %s", exc)
            return
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                direction TEXT NOT NULL,
                last_price REAL,
                confidence REAL,
                reason TEXT,
                created_at REAL
            )
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def save(self, signal: Signal) -> None:
        if self._db is None:
            return
        await self._db.execute(
            "INSERT OR REPLACE INTO signals "
            "(signal_id, symbol, exchange, direction, last_price, confidence, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal.signal_id,
                signal.symbol,
                signal.exchange,
                signal.direction.value,
                signal.last_price,
                signal.confidences.signal,
                json.dumps(signal.reason, ensure_ascii=False),
                signal.created_at or time.time(),
            ),
        )
        await self._db.commit()

    async def recent(self, limit: int = 10) -> List[Dict]:
        if self._db is None:
            return []
        cur = await self._db.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in rows]
