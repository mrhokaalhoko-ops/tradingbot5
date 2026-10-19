"""Durable idempotency and reconciliation store for Demo execution."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any


ACTIVE_STATES = frozenset({
    "CLAIMED", "ENTRY_SUBMITTED", "ENTRY_FILLED_UNPRICED",
    "PARTIALLY_FILLED", "ENTRY_FILLED", "PROTECTION_PENDING", "PROTECTED",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DemoExecutionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS executions (
                candidate_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_demo_execution_state
                ON executions(state, symbol);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def claim_candidate(self, candidate_id: str, symbol: str, payload: dict) -> bool:
        now = _utc_now()
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO executions
                    (candidate_id, symbol, state, payload_json, created_at, updated_at)
                VALUES (?, ?, 'CLAIMED', ?, ?, ?)
                """,
                (candidate_id, symbol, json.dumps(payload, sort_keys=True), now, now),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def update(self, candidate_id: str, state: str, **changes: Any) -> dict:
        with self._lock:
            row = self.get(candidate_id)
            if row is None:
                raise KeyError(candidate_id)
            payload = dict(row["payload"])
            payload.update(changes)
            now = _utc_now()
            self.connection.execute(
                """UPDATE executions SET state=?, payload_json=?, updated_at=?
                   WHERE candidate_id=?""",
                (state, json.dumps(payload, sort_keys=True), now, candidate_id),
            )
            self.connection.commit()
            return {**row, "state": state, "payload": payload, "updated_at": now}

    def get(self, candidate_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM executions WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        return self._decode(row) if row else None

    def active(self) -> list[dict]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        rows = self.connection.execute(
            f"SELECT * FROM executions WHERE state IN ({placeholders}) ORDER BY created_at",
            tuple(sorted(ACTIVE_STATES)),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def all(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM executions ORDER BY created_at"
        ).fetchall()
        return [self._decode(row) for row in rows]

    def event(self, event_type: str, payload: dict, candidate_id: str | None = None) -> None:
        with self._lock:
            self.connection.execute(
                """INSERT INTO events(candidate_id,event_type,payload_json,created_at)
                   VALUES (?,?,?,?)""",
                (candidate_id, event_type, json.dumps(payload, sort_keys=True), _utc_now()),
            )
            self.connection.commit()

    def events(self) -> list[dict]:
        rows = self.connection.execute("SELECT * FROM events ORDER BY id").fetchall()
        return [
            {
                "id": row["id"], "candidate_id": row["candidate_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def put_meta(self, key: str, value: Any) -> None:
        now = _utc_now()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO meta(key,value_json,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, sort_keys=True), now),
            )
            self.connection.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value_json FROM meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value_json"]) if row else default

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        return {
            "candidate_id": row["candidate_id"],
            "symbol": row["symbol"],
            "state": row["state"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

