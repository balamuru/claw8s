"""
claw8s.audit
--------------
SQLite-backed audit log. Every event seen and every action taken is recorded
with full reasoning, outcome, and timestamp. Async via aiosqlite.
"""

import json
import asyncio
import aiosqlite
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class AuditEvent:
    incident_id: str
    timestamp: str
    namespace: str
    object_kind: str
    object_name: str
    reason: str
    message: str
    raw_event: str  # JSON string


@dataclass
class AuditAction:
    incident_id: str
    timestamp: str
    tool_name: str
    tool_args: str       # JSON string
    reasoning: str
    confidence: float
    status: ActionStatus
    source: str          # "skill" or "soul"
    result: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0


class AuditLog:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        # Increase timeout to 20s to prevent deadlocks during high dashboard activity
        self._db = await aiosqlite.connect(self.db_path, timeout=20.0)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._create_tables()

    async def close(self):
        if self._db:
            await self._db.close()

    async def _create_tables(self):
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                namespace   TEXT NOT NULL,
                object_kind TEXT NOT NULL,
                object_name TEXT NOT NULL,
                reason      TEXT NOT NULL,
                message     TEXT NOT NULL,
                raw_event   TEXT NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                tool_name   TEXT NOT NULL,
                tool_args   TEXT NOT NULL,
                reasoning   TEXT NOT NULL,
                confidence  REAL NOT NULL,
                status      TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'soul',
                result      TEXT,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0
            )
        """)
        try:
            await self._db.execute("ALTER TABLE actions ADD COLUMN source TEXT NOT NULL DEFAULT 'soul'")
        except:
            pass
        try:
            await self._db.execute("ALTER TABLE actions ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0")
            await self._db.execute("ALTER TABLE actions ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0")
        except:
            pass
        await self._db.commit()

    async def log_event(self, event: AuditEvent):
        await self._db.execute("""
            INSERT INTO events (incident_id, timestamp, namespace, object_kind, object_name, reason, message, raw_event)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.incident_id, event.timestamp, event.namespace,
            event.object_kind, event.object_name, event.reason,
            event.message, event.raw_event
        ))
        await self._db.commit()

    async def log_action(self, action: AuditAction):
        await self._db.execute("""
            INSERT INTO actions (incident_id, timestamp, tool_name, tool_args, reasoning, confidence, status, source, result, input_tokens, output_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            action.incident_id, action.timestamp, action.tool_name,
            action.tool_args, action.reasoning, action.confidence,
            action.status.value, action.source, action.result,
            action.input_tokens, action.output_tokens
        ))
        await self._db.commit()

    async def update_action_result(self, incident_id: str, tool_name: str, status: ActionStatus, result: str):
        await self._db.execute("""
            UPDATE actions SET status=?, result=?
            WHERE incident_id=? AND tool_name=?
            ORDER BY id DESC LIMIT 1
        """, (status.value, result, incident_id, tool_name))
        await self._db.commit()

    async def get_recent_incidents(self, limit: int = 10) -> list[dict]:
        async with self._db.execute("""
            SELECT DISTINCT incident_id, MAX(timestamp) as ts, namespace, object_kind, object_name, reason
            FROM events GROUP BY incident_id ORDER BY ts DESC LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [
                {"incident_id": r[0], "timestamp": r[1], "namespace": r[2],
                 "object_kind": r[3], "object_name": r[4], "reason": r[5]}
                for r in rows
            ]

    async def get_dashboard_data(self, limit: int = 50) -> list[dict]:
        """Fetch joined data for the dashboard."""
        query = """
            SELECT 
                e.incident_id, 
                MAX(e.timestamp) as ts, 
                e.namespace, 
                e.object_kind, 
                e.object_name, 
                e.reason,
                (SELECT status FROM actions WHERE incident_id = e.incident_id ORDER BY id DESC LIMIT 1) as last_status,
                (SELECT source FROM actions WHERE incident_id = e.incident_id ORDER BY id DESC LIMIT 1) as last_source,
                (SELECT SUM(input_tokens + output_tokens) FROM actions WHERE incident_id = e.incident_id) as total_tokens
            FROM events e
            GROUP BY e.incident_id
            ORDER BY ts DESC
            LIMIT ?
        """
        async with self._db.execute(query, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "incident_id": r[0], 
                    "timestamp": r[1], 
                    "namespace": r[2],
                    "object_kind": r[3], 
                    "object_name": r[4], 
                    "reason": r[5],
                    "status": r[6] or "pending",
                    "source": r[7] or "unknown",
                    "total_tokens": r[8] or 0
                }
                for r in rows
            ]

    async def get_incident_actions(self, incident_id: str) -> list[dict]:
        """Fetch all actions taken for a specific incident."""
        async with self._db.execute("""
            SELECT tool_name, tool_args, reasoning, confidence, status, result, timestamp, source, input_tokens, output_tokens
            FROM actions WHERE incident_id=? ORDER BY id
        """, (incident_id,)) as cursor:
            rows = await cursor.fetchall()
            return [
                {"tool": r[0], "args": r[1], "reasoning": r[2],
                 "confidence": r[3], "status": r[4], "result": r[5], 
                 "timestamp": r[6], "source": r[7], "input_tokens": r[8], "output_tokens": r[9]}
                for r in rows
            ]

    async def get_recent_object_actions(self, namespace: str, kind: str, name: str, hours: int = 2) -> list[dict]:
        """Fetch all autonomous actions taken on this specific object recently."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with self._db.execute("""
            SELECT a.tool_name, a.status, a.result, a.reasoning, a.timestamp, e.reason
            FROM actions a
            JOIN events e ON a.incident_id = e.id
            WHERE e.namespace = ? AND e.object_kind = ? AND e.object_name = ?
              AND a.timestamp > ?
            ORDER BY a.timestamp DESC
        """, (namespace, kind, name, cutoff)) as cursor:
            rows = await cursor.fetchall()
            return [
                {"tool": r[0], "status": r[1], "result": r[2], "reasoning": r[3], "timestamp": r[4], "incident_reason": r[5]}
                for r in rows
            ]

    async def get_incident_frequency(self, bucket_minutes: int = 60) -> list[dict]:
        """Group incidents into time buckets for the histogram."""
        query = f"""
            SELECT 
                strftime('%Y-%m-%dT%H:%M:00Z', datetime((strftime('%s', timestamp) / ({bucket_minutes} * 60)) * ({bucket_minutes} * 60), 'unixepoch')) as bucket,
                count(*) as count
            FROM events
            GROUP BY bucket
            ORDER BY bucket DESC
            LIMIT 24
        """
        async with self._db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [{"bucket": r[0], "count": r[1]} for r in rows]

    async def purge_old_records(self, days: int):
        """Delete incidents and actions older than N days."""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        
        # 1. Delete actions for old incidents
        await self._db.execute("""
            DELETE FROM actions WHERE incident_id IN (
                SELECT id FROM events WHERE timestamp < ?
            )
        """, (cutoff,))
        
        # 2. Delete old incidents
        async with self._db.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,)) as cursor:
            deleted_count = cursor.rowcount
            
        await self._db.commit()
        if deleted_count > 0:
            import logging
            logging.getLogger("audit").info(f"Purged {deleted_count} stale incidents older than {days} days.")
        return deleted_count

    async def clear_all_records(self):
        """Wipe all audit history."""
        import logging
        log = logging.getLogger("audit")
        try:
            await self._db.execute("DELETE FROM actions")
            await self._db.execute("DELETE FROM events")
            await self._db.commit()
            log.info("Audit history cleared successfully via API.")
            return True
        except Exception as e:
            log.error(f"Failed to clear audit history: {e}")
            return False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
