from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from cryptopilot.models import Signal


class SignalStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    confidence INTEGER NOT NULL,
                    score REAL NOT NULL,
                    price REAL NOT NULL,
                    fingerprint TEXT NOT NULL,
                    actionable INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_signals_symbol
                    ON signals(exchange, symbol, created_at DESC);
                CREATE TABLE IF NOT EXISTS alerts (
                    fingerprint TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL,
                    price REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            await db.commit()

    async def save(self, signal: Signal) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO signals
                    (created_at, exchange, symbol, side, confidence, score, price,
                     fingerprint, actionable, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.created_at.astimezone(UTC).isoformat(),
                    signal.exchange,
                    signal.symbol,
                    signal.side.value,
                    signal.confidence,
                    signal.score,
                    signal.price,
                    signal.fingerprint,
                    int(signal.actionable),
                    json.dumps(signal.to_dict(), ensure_ascii=False),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def should_alert(self, signal: Signal, cooldown_minutes: int) -> bool:
        threshold = datetime.now(UTC) - timedelta(minutes=cooldown_minutes)
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT sent_at, price FROM alerts WHERE fingerprint = ?", (signal.fingerprint,)
                )
            ).fetchone()
        if row is None:
            return True
        sent_at = datetime.fromisoformat(row[0])
        previous_price = float(row[1])
        moved = abs(signal.price - previous_price) / max(previous_price, 1e-12) >= 0.01
        return sent_at < threshold or moved

    async def mark_alerted(self, signal: Signal) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO alerts (fingerprint, sent_at, price) VALUES (?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE
                    SET sent_at=excluded.sent_at, price=excluded.price
                """,
                (signal.fingerprint, now, signal.price),
            )
            await db.commit()

    async def recent(self, limit: int = 10, actionable_only: bool = True) -> list[dict]:
        where = "WHERE actionable = 1" if actionable_only else ""
        async with aiosqlite.connect(self.path) as db:
            rows = await (
                await db.execute(
                    f"SELECT payload FROM signals {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                    (min(max(limit, 1), 50),),
                )
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    async def set_runtime(self, key: str, value: str) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO runtime (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )
            await db.commit()

    async def get_runtime(self, key: str) -> tuple[str, str] | None:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute("SELECT value, updated_at FROM runtime WHERE key = ?", (key,))
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None
