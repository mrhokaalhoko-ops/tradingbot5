"""Bounded, append-friendly persistence for Strategy V2 shadow evidence.

This module is deliberately independent from ``StateManager`` and therefore
never serializes or mutates ``bot_state.json``.  It uses only Python's stdlib
SQLite driver, has no exchange dependency, and exposes no trading operation.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import threading
from typing import Any, Mapping


DEFAULT_LIMITS = {
    "decisions": 500,
    "management": 20_000,
    "outcomes": 500,
    "theses": 1_000,
    "resets": 2_000,
}
DEFAULT_MAX_BYTES = 128 * 1024 * 1024


def _json(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    encoded = _json(value if isinstance(value, (dict, list)) else {"value": value}).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ShadowEvidenceStore:
    """SQLite-backed research store with hard global retention and size caps."""

    def __init__(self, directory: str, *, limits: Mapping[str, int] | None = None,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self.directory = os.path.abspath(directory)
        self.path = os.path.join(self.directory, "strategy_v2_shadow.sqlite3")
        self.limits = {**DEFAULT_LIMITS, **dict(limits or {})}
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._lock = threading.RLock()
        self._connections: set[sqlite3.Connection] = set()
        self._closed = False
        os.makedirs(self.directory, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise RuntimeError("shadow evidence store is closed")
            connection = sqlite3.connect(self.path, timeout=15.0)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=15000")
                connection.execute("PRAGMA wal_autocheckpoint=1000")
            except Exception:
                connection.close()
                raise
            self._connections.add(connection)
            return connection

    def _release_connection(self, connection: sqlite3.Connection) -> None:
        try:
            connection.close()
        finally:
            with self._lock:
                self._connections.discard(connection)

    @contextmanager
    def _connection(self):
        """Transaction scope that always closes its SQLite file handle."""
        connection = self._connect()
        try:
            yield connection
            if connection.in_transaction:
                connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._release_connection(connection)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        """Close every active handle. Safe to call more than once."""
        with self._lock:
            if self._closed and not self._connections:
                return
            self._closed = True
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except sqlite3.Error:
                pass
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def __enter__(self):
        if self.closed:
            raise RuntimeError("shadow evidence store is closed")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    proposal_id TEXT PRIMARY KEY,
                    event_time TEXT NOT NULL,
                    symbol TEXT,
                    record_hash TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    proposal_id TEXT PRIMARY KEY,
                    trade_id TEXT,
                    event_time TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS outcomes_trade_id_idx ON outcomes(trade_id);
                CREATE TABLE IF NOT EXISTS management (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    dedup_hash TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS management_trade_idx
                    ON management(trade_id, id DESC);
                CREATE TABLE IF NOT EXISTS theses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT,
                    thesis_id TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    dedup_hash TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS theses_lookup_idx
                    ON theses(symbol, side, id DESC);
                CREATE TABLE IF NOT EXISTS resets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    dedup_hash TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )
            result = connection.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("shadow evidence database failed quick_check")

    def _increment_metric(self, connection: sqlite3.Connection, key: str, amount: int) -> None:
        connection.execute(
            "INSERT INTO metrics(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
            (key, int(amount)),
        )

    def _file_footprint(self) -> int:
        return sum(
            os.path.getsize(path) for path in (self.path, self.path + "-wal", self.path + "-shm")
            if os.path.exists(path)
        )

    def _prune_table(self, connection: sqlite3.Connection, table: str,
                     limit: int, order_column: str = "rowid") -> int:
        before = connection.total_changes
        connection.execute(
            f"DELETE FROM {table} WHERE {order_column} IN ("
            f"SELECT {order_column} FROM {table} ORDER BY {order_column} DESC "
            f"LIMIT -1 OFFSET ?)",
            (max(0, int(limit)),),
        )
        return connection.total_changes - before

    def _enforce_retention(self, connection: sqlite3.Connection) -> int:
        removed = 0
        removed += self._prune_table(connection, "decisions", self.limits["decisions"], "rowid")
        removed += self._prune_table(connection, "management", self.limits["management"], "id")
        removed += self._prune_table(connection, "outcomes", self.limits["outcomes"], "rowid")
        removed += self._prune_table(connection, "theses", self.limits["theses"], "id")
        removed += self._prune_table(connection, "resets", self.limits["resets"], "id")
        if removed:
            self._increment_metric(connection, "records_pruned", removed)
        return removed

    def _enforce_size_ceiling(self) -> None:
        if self._file_footprint() <= self.max_bytes:
            return
        with self._lock, self._connection() as connection:
            # Management/reset evidence is intentionally the first data pruned.
            # Keep at least a small recent sample of every class.
            for table, order_column, floor in (
                ("management", "id", 100),
                ("resets", "id", 50),
                ("theses", "id", 50),
                ("outcomes", "rowid", 25),
                ("decisions", "rowid", 25),
            ):
                count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if count > floor:
                    keep = max(floor, int(count * 0.75))
                    removed = self._prune_table(connection, table, keep, order_column)
                    if removed:
                        self._increment_metric(connection, "size_ceiling_pruned", removed)
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA incremental_vacuum")
                if self._file_footprint() <= self.max_bytes:
                    return
        if self._file_footprint() > self.max_bytes:
            raise RuntimeError("SHADOW_STORAGE_HARD_CEILING_REACHED")

    def _finish_write(self, connection: sqlite3.Connection, payload_bytes: int) -> None:
        self._increment_metric(connection, "write_transactions", 1)
        self._increment_metric(connection, "approx_payload_bytes", payload_bytes)
        self._enforce_retention(connection)
        connection.commit()
        self._enforce_size_ceiling()

    def save_strategy_v2_shadow_decision(self, record: Mapping[str, Any]) -> bool:
        proposal_id = str(record.get("proposal_id") or "").strip()
        record_hash = str(record.get("decision_record_hash") or "").strip()
        if not proposal_id or not record_hash or record.get("execution_authority") is not False:
            return False
        payload = _json(dict(record))
        with self._lock, self._connection() as connection:
            prior = connection.execute(
                "SELECT record_hash FROM decisions WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if prior:
                return str(prior[0]) == record_hash
            connection.execute(
                "INSERT INTO decisions(proposal_id,event_time,symbol,record_hash,payload) "
                "VALUES(?,?,?,?,?)",
                (proposal_id, str(record.get("decision_timestamp") or _utc_now()),
                 str(record.get("symbol") or ""), record_hash, payload),
            )
            for reset in list(record.get("reentry_reset_evidence") or []):
                reset_payload = {
                    "proposal_id": proposal_id,
                    "evidence": copy.deepcopy(reset),
                    "execution_authority": False,
                }
                digest = _hash(reset_payload)
                connection.execute(
                    "INSERT OR IGNORE INTO resets(proposal_id,event_time,dedup_hash,payload) "
                    "VALUES(?,?,?,?)",
                    (proposal_id, str(reset.get("timestamp") or record.get("decision_timestamp") or _utc_now()),
                     digest, _json(reset_payload)),
                )
            self._finish_write(connection, len(payload.encode("utf-8")))
        return True

    def get_strategy_v2_shadow_decision(self, proposal_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM decisions WHERE proposal_id=?", (str(proposal_id),)
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def record_strategy_v2_shadow_outcome(self, proposal_id: str,
                                          outcome: Mapping[str, Any]) -> bool:
        proposal_id = str(proposal_id or "").strip()
        if not proposal_id or outcome.get("execution_authority") is not False:
            return False
        payload = _json(dict(outcome))
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO outcomes(proposal_id,trade_id,event_time,payload) VALUES(?,?,?,?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET trade_id=excluded.trade_id, "
                "event_time=excluded.event_time, payload=excluded.payload",
                (proposal_id, str(outcome.get("trade_id") or ""),
                 str(outcome.get("recorded_at") or _utc_now()), payload),
            )
            self._finish_write(connection, len(payload.encode("utf-8")))
        return True

    def update_strategy_v2_shadow_post_exit(self, trade_id: str, horizon: str,
                                            checkpoint: Mapping[str, Any]) -> bool:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT proposal_id,payload FROM outcomes WHERE trade_id=? "
                "ORDER BY rowid DESC LIMIT 1", (str(trade_id or ""),)
            ).fetchone()
            if not row:
                return False
            outcome = json.loads(row[1])
            outcome.setdefault("post_exit_MFE", {})[str(horizon)] = copy.deepcopy(dict(checkpoint))
            payload = _json(outcome)
            connection.execute(
                "UPDATE outcomes SET event_time=?,payload=? WHERE proposal_id=?",
                (_utc_now(), payload, row[0]),
            )
            self._finish_write(connection, len(payload.encode("utf-8")))
        return True

    def append_strategy_v2_management_shadow(self, trade_id: str,
                                             record: Mapping[str, Any]) -> bool:
        if not trade_id or record.get("execution_authority") is not False:
            return False
        payload = _json(dict(record))
        identity = {
            "trade_id": str(trade_id),
            "timestamp": record.get("timestamp"),
            "structure_confirmed_at": record.get("structure_confirmed_at"),
            "candidate_stop": record.get("candidate_stop"),
            "reason": record.get("reason"),
        }
        digest = _hash(identity)
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO management(trade_id,event_time,dedup_hash,payload) "
                "VALUES(?,?,?,?)",
                (str(trade_id), str(record.get("timestamp") or _utc_now()), digest, payload),
            )
            self._finish_write(connection, len(payload.encode("utf-8")))
        return True

    def get_latest_strategy_v2_management_shadow(self, trade_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM management WHERE trade_id=? ORDER BY id DESC LIMIT 1",
                (str(trade_id or ""),),
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def record_closed_thesis(self, symbol: str, thesis_id: str, *, side: str,
                             closed_at: str, details: Mapping[str, Any] | None = None) -> bool:
        if not symbol or not thesis_id or not closed_at:
            return False
        record = {
            "symbol": str(symbol).upper(), "thesis_id": str(thesis_id),
            "side": str(side or "").upper(), "closed_at": str(closed_at),
            "details": copy.deepcopy(dict(details or {})),
            "execution_authority": False,
        }
        payload, digest = _json(record), _hash(record)
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO theses(symbol,side,thesis_id,closed_at,dedup_hash,payload) "
                "VALUES(?,?,?,?,?,?)",
                (record["symbol"], record["side"], record["thesis_id"],
                 record["closed_at"], digest, payload),
            )
            self._finish_write(connection, len(payload.encode("utf-8")))
        return True

    def get_latest_closed_thesis(self, symbol: str, side: str = "") -> dict[str, Any]:
        query = "SELECT payload FROM theses WHERE symbol=?"
        params: list[Any] = [str(symbol or "").upper()]
        if side:
            query += " AND side=?"
            params.append(str(side).upper())
        query += " ORDER BY id DESC LIMIT 1"
        with self._lock, self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return json.loads(row[0]) if row else {}

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("decisions", "management", "outcomes", "theses", "resets")
            }
            metrics = {
                str(row[0]): int(row[1]) for row in connection.execute(
                    "SELECT key,value FROM metrics"
                ).fetchall()
            }
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        return {
            "path": self.path,
            "counts": counts,
            "limits": dict(self.limits),
            "max_bytes": self.max_bytes,
            "disk_bytes": self._file_footprint(),
            "metrics": metrics,
            "quick_check": quick_check,
        }


_STORE_LOCK = threading.Lock()
_STORES: dict[tuple[str, int], ShadowEvidenceStore] = {}


def get_shadow_evidence_store(directory: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> ShadowEvidenceStore:
    key = (os.path.abspath(directory), int(max_bytes))
    with _STORE_LOCK:
        store = _STORES.get(key)
        if store is None or store.closed:
            store = ShadowEvidenceStore(key[0], max_bytes=key[1])
            _STORES[key] = store
        return store
