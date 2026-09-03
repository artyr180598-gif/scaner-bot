"""SQLite persistence layer (aiosqlite, cheap migration to PostgreSQL later).

Abstraction is deliberately thin — only what the bot needs. The repository is
kept free of SQLAlchemy by default so Railway can start with zero external
state, but switching to Postgres later means adding a connection URL and
replacing ``aiosqlite`` with ``asyncpg`` in one place.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from loguru import logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    risk_profile TEXT NOT NULL DEFAULT 'balanced',
    min_confidence INTEGER NOT NULL DEFAULT 62,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    entry_low REAL NOT NULL,
    entry_high REAL NOT NULL,
    stop_loss REAL NOT NULL,
    tp1 REAL NOT NULL,
    tp2 REAL NOT NULL,
    tp3 REAL NOT NULL,
    rr1 REAL NOT NULL,
    rr2 REAL NOT NULL,
    rr3 REAL NOT NULL,
    confidence REAL NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    rationale TEXT NOT NULL DEFAULT '',
    risks TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    target_above REAL,
    target_below REAL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alerts_chat ON alerts(chat_id, active);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info("Database initialized at {}", self.path)

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- users ---------------------------------------------------------------
    async def get_user(self, chat_id: int) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_user(
        self,
        chat_id: int,
        risk_profile: str,
        min_confidence: int,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO users (chat_id, risk_profile, min_confidence, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    risk_profile = excluded.risk_profile,
                    min_confidence = excluded.min_confidence,
                    updated_at = excluded.updated_at
                """,
                (chat_id, risk_profile, min_confidence, int(time.time())),
            )
            await self.conn.commit()

    async def update_user_settings(
        self,
        chat_id: int,
        risk_profile: Optional[str] = None,
        min_confidence: Optional[int] = None,
    ) -> None:
        user = await self.get_user(chat_id)
        if user is None:
            await self.upsert_user(
                chat_id,
                risk_profile or "balanced",
                min_confidence or 62,
            )
            return
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE users SET
                    risk_profile = ?,
                    min_confidence = ?,
                    updated_at = ?
                WHERE chat_id = ?
                """,
                (
                    risk_profile or user["risk_profile"],
                    min_confidence or user["min_confidence"],
                    int(time.time()),
                    chat_id,
                ),
            )
            await self.conn.commit()

    # -- signals ----------------------------------------------------------------
    async def save_signal(self, signal: Any) -> int:
        async with self._write_lock:
            cur = await self.conn.execute(
                """
                INSERT INTO signals (
                    symbol, direction, timeframe, entry_low, entry_high, stop_loss,
                    tp1, tp2, tp3, rr1, rr2, rr3, confidence, score,
                    rationale, risks, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    signal.symbol,
                    signal.direction,
                    signal.timeframe,
                    signal.entry_low,
                    signal.entry_high,
                    signal.stop_loss,
                    signal.tp1,
                    signal.tp2,
                    signal.tp3,
                    signal.rr1,
                    signal.rr2,
                    signal.rr3,
                    signal.confidence,
                    signal.score,
                    signal.rationale,
                    signal.risks,
                    int(time.time()),
                ),
            )
            await self.conn.commit()
            return int(cur.lastrowid or 0)

    async def get_recent_signals(self, limit: int = 10) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- price alerts ---------------------------------------------------------
    async def add_alert(
        self,
        chat_id: int,
        symbol: str,
        target_above: float | None,
        target_below: float | None,
    ) -> int:
        async with self._write_lock:
            cur = await self.conn.execute(
                """
                INSERT INTO alerts (
                    chat_id, symbol, target_above, target_below,
                    active, created_at, last_checked_at
                ) VALUES (?, ?, ?, ?, 1, ?, 0)
                """,
                (chat_id, symbol.upper(), target_above, target_below, int(time.time())),
            )
            await self.conn.commit()
            return int(cur.lastrowid or 0)

    async def list_alerts(self, chat_id: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM alerts WHERE chat_id = ? AND active = 1 ORDER BY created_at DESC",
            (chat_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_active_alerts(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM alerts WHERE active = 1 ORDER BY id ASC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def deactivate_alert(self, alert_id: int) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE alerts SET active = 0 WHERE id = ?", (alert_id,)
            )
            await self.conn.commit()

    async def mark_alerts_checked(self, alert_ids: list[int]) -> None:
        if not alert_ids:
            return
        async with self._write_lock:
            await self.conn.execute(
                f"UPDATE alerts SET last_checked_at = ? WHERE id IN ({','.join('?' * len(alert_ids))})",
                (int(time.time()), *alert_ids),
            )
            await self.conn.commit()
