from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from cryptopilot.models import CalibrationStats, PaperTrade, Side, Signal


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
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    side TEXT NOT NULL,
                    confidence INTEGER NOT NULL,
                    regime TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    entry_expires_at TEXT NOT NULL,
                    exit_expires_at TEXT NOT NULL,
                    entry_low REAL NOT NULL,
                    entry_high REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    status TEXT NOT NULL,
                    entry_price REAL,
                    entry_at TEXT,
                    exit_price REAL,
                    closed_at TEXT,
                    result_r REAL,
                    outcome TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_paper_open
                    ON paper_trades(status, exchange, symbol);
                CREATE INDEX IF NOT EXISTS idx_paper_calibration
                    ON paper_trades(symbol, side, id DESC);
                CREATE TABLE IF NOT EXISTS flow_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    bias TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    event_price REAL NOT NULL,
                    trigger_price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    resolved_at TEXT,
                    triggered_at TEXT,
                    lead_seconds REAL
                );
                CREATE INDEX IF NOT EXISTS idx_flow_observations_status
                    ON flow_observations(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_flow_observations_created
                    ON flow_observations(created_at DESC);
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
        return await self.should_alert_event(
            signal.fingerprint, signal.price, cooldown_minutes
        )

    async def should_alert_event(
        self, fingerprint: str, price: float, cooldown_minutes: int
    ) -> bool:
        threshold = datetime.now(UTC) - timedelta(minutes=cooldown_minutes)
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT sent_at, price FROM alerts WHERE fingerprint = ?", (fingerprint,)
                )
            ).fetchone()
        if row is None:
            return True
        sent_at = datetime.fromisoformat(row[0])
        previous_price = float(row[1])
        moved = abs(price - previous_price) / max(previous_price, 1e-12) >= 0.01
        return sent_at < threshold or moved

    async def mark_event_alerted(self, fingerprint: str, price: float) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO alerts (fingerprint, sent_at, price) VALUES (?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE
                    SET sent_at=excluded.sent_at, price=excluded.price
                """,
                (fingerprint, now, price),
            )
            await db.commit()

    async def mark_alerted(
        self,
        signal: Signal,
        *,
        track_paper: bool = True,
        max_holding_hours: int = 72,
    ) -> None:
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
            if track_paper and signal.plan is not None:
                exit_expires = signal.created_at + timedelta(hours=max_holding_hours)
                await db.execute(
                    """
                    INSERT INTO paper_trades
                        (symbol, exchange, side, confidence, regime, strategy_version,
                         created_at, entry_expires_at, exit_expires_at, entry_low,
                         entry_high, stop_loss, take_profit, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WAITING')
                    """,
                    (
                        signal.symbol,
                        signal.exchange,
                        signal.side.value,
                        signal.confidence,
                        signal.regime,
                        signal.strategy_version,
                        signal.created_at.astimezone(UTC).isoformat(),
                        signal.plan.expires_at.astimezone(UTC).isoformat(),
                        exit_expires.astimezone(UTC).isoformat(),
                        signal.plan.entry_low,
                        signal.plan.entry_high,
                        signal.plan.stop_loss,
                        signal.plan.take_profit_2,
                    ),
                )
            await db.commit()

    async def open_paper_trades(self) -> list[PaperTrade]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT id, symbol, exchange, side, confidence, regime, created_at,
                           entry_expires_at, exit_expires_at, entry_low, entry_high,
                           stop_loss, take_profit, status, entry_price, entry_at
                    FROM paper_trades
                    WHERE status IN ('WAITING', 'OPEN')
                    ORDER BY id
                    """
                )
            ).fetchall()
        return [
            PaperTrade(
                id=int(row[0]),
                symbol=str(row[1]),
                exchange=str(row[2]),
                side=Side(str(row[3])),
                confidence=int(row[4]),
                regime=str(row[5]),
                created_at=datetime.fromisoformat(row[6]),
                entry_expires_at=datetime.fromisoformat(row[7]),
                exit_expires_at=datetime.fromisoformat(row[8]),
                entry_low=float(row[9]),
                entry_high=float(row[10]),
                stop_loss=float(row[11]),
                take_profit=float(row[12]),
                status=str(row[13]),
                entry_price=float(row[14]) if row[14] is not None else None,
                entry_at=datetime.fromisoformat(row[15]) if row[15] else None,
            )
            for row in rows
        ]

    async def mark_paper_entry(
        self, trade_id: int, entry_price: float, entered_at: datetime
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE paper_trades
                SET status='OPEN', entry_price=?, entry_at=?
                WHERE id=? AND status='WAITING'
                """,
                (entry_price, entered_at.astimezone(UTC).isoformat(), trade_id),
            )
            await db.commit()

    async def close_paper_trade(
        self,
        trade_id: int,
        *,
        outcome: str,
        result_r: float | None,
        exit_price: float | None,
        closed_at: datetime,
        status: str = "CLOSED",
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE paper_trades
                SET status=?, outcome=?, result_r=?, exit_price=?, closed_at=?
                WHERE id=? AND status IN ('WAITING', 'OPEN')
                """,
                (
                    status,
                    outcome,
                    result_r,
                    exit_price,
                    closed_at.astimezone(UTC).isoformat(),
                    trade_id,
                ),
            )
            await db.commit()

    async def active_paper_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE status IN ('WAITING', 'OPEN')"
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def calibration(
        self,
        *,
        symbol: str | None = None,
        side: Side | None = None,
        limit: int = 100,
    ) -> CalibrationStats:
        clauses = ["status='CLOSED'", "result_r IS NOT NULL"]
        parameters: list[object] = []
        if symbol:
            clauses.append("symbol=?")
            parameters.append(symbol)
        if side:
            clauses.append("side=?")
            parameters.append(side.value)
        parameters.append(min(max(limit, 1), 1000))
        query = f"""
            SELECT result_r FROM paper_trades
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC LIMIT ?
        """
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(query, parameters)).fetchall()
        values = [float(row[0]) for row in rows]
        return _calibration_stats(values)

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

    async def record_flow_observation(
        self,
        *,
        symbol: str,
        bias: Side,
        score: int,
        event_type: str,
        event_price: float,
        trigger_price: float,
        created_at: datetime,
        window_minutes: int,
    ) -> int:
        expires_at = created_at + timedelta(minutes=window_minutes)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO flow_observations
                    (created_at, expires_at, symbol, bias, score, event_type,
                     event_price, trigger_price, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                (
                    created_at.astimezone(UTC).isoformat(),
                    expires_at.astimezone(UTC).isoformat(),
                    symbol,
                    bias.value,
                    score,
                    event_type,
                    event_price,
                    trigger_price,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def pending_flow_observations(self, limit: int = 100) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT id, created_at, expires_at, symbol, bias, score,
                           event_type, event_price, trigger_price
                    FROM flow_observations
                    WHERE status='PENDING'
                    ORDER BY id
                    LIMIT ?
                    """,
                    (min(max(limit, 1), 500),),
                )
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "created_at": datetime.fromisoformat(row[1]),
                "expires_at": datetime.fromisoformat(row[2]),
                "symbol": str(row[3]),
                "bias": Side(str(row[4])),
                "score": int(row[5]),
                "event_type": str(row[6]),
                "event_price": float(row[7]),
                "trigger_price": float(row[8]),
            }
            for row in rows
        ]

    async def resolve_flow_observation(
        self,
        observation_id: int,
        *,
        status: str,
        resolved_at: datetime,
        triggered_at: datetime | None = None,
        lead_seconds: float | None = None,
    ) -> None:
        if status not in {"TRIGGERED", "EXPIRED"}:
            raise ValueError("Flow observation status must be TRIGGERED or EXPIRED")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE flow_observations
                SET status=?, resolved_at=?, triggered_at=?, lead_seconds=?
                WHERE id=? AND status='PENDING'
                """,
                (
                    status,
                    resolved_at.astimezone(UTC).isoformat(),
                    triggered_at.astimezone(UTC).isoformat() if triggered_at else None,
                    lead_seconds,
                    observation_id,
                ),
            )
            await db.commit()

    async def flow_validation_stats(self, limit: int = 200) -> dict[str, float | int | None]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT status, lead_seconds
                    FROM flow_observations
                    WHERE status IN ('TRIGGERED', 'EXPIRED')
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (min(max(limit, 1), 2000),),
                )
            ).fetchall()
            pending_row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM flow_observations WHERE status='PENDING'"
                )
            ).fetchone()
        resolved = len(rows)
        triggered = sum(str(row[0]) == "TRIGGERED" for row in rows)
        leads = [
            float(row[1])
            for row in rows
            if str(row[0]) == "TRIGGERED" and row[1] is not None
        ]
        return {
            "resolved": resolved,
            "triggered": triggered,
            "expired": resolved - triggered,
            "pending": int(pending_row[0]) if pending_row else 0,
            "trigger_rate_pct": triggered / resolved * 100 if resolved else None,
            "median_lead_seconds": statistics.median(leads) if leads else None,
        }

    async def strict_alert_allowed(self, fingerprint: str, cooldown_minutes: int) -> bool:
        threshold = datetime.now(UTC) - timedelta(minutes=cooldown_minutes)
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT sent_at FROM alerts WHERE fingerprint = ?",
                    (fingerprint,),
                )
            ).fetchone()
        if row is None:
            return True
        return datetime.fromisoformat(str(row[0])) < threshold

    async def notification_budget_available(
        self,
        key: str,
        *,
        cooldown_minutes: int,
        max_per_day: int,
    ) -> bool:
        now = datetime.now(UTC)
        row = await self.get_runtime(f"notification_budget:{key}")
        if row is None:
            return True
        try:
            payload = json.loads(row[0])
            last_sent = datetime.fromisoformat(str(payload["last_sent"]))
            sent_date = str(payload["date"])
            count = int(payload["count"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return True
        if sent_date != now.date().isoformat():
            return True
        if count >= max_per_day:
            return False
        return last_sent <= now - timedelta(minutes=cooldown_minutes)

    async def mark_notification_budget(self, key: str) -> None:
        now = datetime.now(UTC)
        runtime_key = f"notification_budget:{key}"
        row = await self.get_runtime(runtime_key)
        count = 0
        if row is not None:
            try:
                payload = json.loads(row[0])
                if str(payload.get("date")) == now.date().isoformat():
                    count = int(payload.get("count", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                count = 0
        await self.set_runtime(
            runtime_key,
            json.dumps(
                {
                    "date": now.date().isoformat(),
                    "count": count + 1,
                    "last_sent": now.isoformat(),
                }
            ),
        )

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


def _calibration_stats(values: list[float]) -> CalibrationStats:
    sample_size = len(values)
    wins = sum(value > 0 for value in values)
    losses = sample_size - wins
    win_rate = wins / sample_size if sample_size else 0.0
    low, high = _wilson_interval(wins, sample_size)
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = abs(sum(value for value in values if value <= 0))
    return CalibrationStats(
        sample_size=sample_size,
        wins=wins,
        losses=losses,
        win_rate=win_rate * 100,
        interval_low=low * 100,
        interval_high=high * 100,
        expectancy_r=sum(values) / sample_size if sample_size else 0.0,
        profit_factor=(
            gross_profit / gross_loss if gross_loss else math.inf if gross_profit else 0.0
        ),
    )


def _wilson_interval(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    probability = wins / total
    denominator = 1 + z * z / total
    centre = probability + z * z / (2 * total)
    margin = z * math.sqrt(
        probability * (1 - probability) / total + z * z / (4 * total * total)
    )
    return max(0.0, (centre - margin) / denominator), min(
        1.0, (centre + margin) / denominator
    )
