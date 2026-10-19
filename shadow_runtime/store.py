"""Transactional, restart-safe SQLite ledger for prospective Shadow events."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ShadowStore:
    """One-writer ledger with database-enforced deduplication.

    No method in this class knows about an exchange, API key, order, or account.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _migrate(self) -> None:
        # sqlite3.executescript controls its own transaction boundary; wrapping
        # it in BEGIN/COMMIT causes an implicit commit before our COMMIT.
        with self._lock:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    emitted_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policy_events (
                    event_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    policy_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    thesis_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    emitted_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_policy_events_thesis
                    ON policy_events(policy_id, thesis_id, emitted_at);
                CREATE TABLE IF NOT EXISTS theses (
                    policy_id TEXT NOT NULL,
                    thesis_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(policy_id, thesis_id),
                    UNIQUE(policy_id, candidate_id)
                );
                CREATE TABLE IF NOT EXISTS positions (
                    policy_id TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    thesis_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(policy_id, position_id),
                    UNIQUE(policy_id, thesis_id)
                );
                CREATE INDEX IF NOT EXISTS idx_positions_open
                    ON positions(policy_id, status, opened_at);
                CREATE TABLE IF NOT EXISTS execution_quotes (
                    event_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    update_id INTEGER NOT NULL,
                    best_bid REAL NOT NULL,
                    best_ask REAL NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(symbol, update_id, event_time)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_quotes_symbol_time
                    ON execution_quotes(symbol, event_time, update_id);
                CREATE TABLE IF NOT EXISTS candle_open_events (
                    event_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    open_time TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    open_price REAL NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(symbol, open_time)
                );
                COMMIT;
                """
            )

    def put_meta(self, key: str, value: Any) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO runtime_meta(key,value_json,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                updated_at=excluded.updated_at""",
                (key, _json(value), _utc_now()),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self._connection.execute(
            "SELECT value_json FROM runtime_meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def insert_snapshot(
        self, snapshot_id: str, symbol: str, as_of: str, payload: dict, payload_hash: str
    ) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO market_snapshots VALUES(?,?,?,?,?)",
                (snapshot_id, symbol, as_of, _json(payload), payload_hash),
            )
            if cursor.rowcount == 0:
                existing = db.execute(
                    "SELECT payload_hash FROM market_snapshots WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()
                if not existing or existing[0] != payload_hash:
                    raise RuntimeError("snapshot id collision with different payload")
                return False
            return True

    def insert_candidate(self, candidate_id: str, symbol: str, emitted_at: str, payload: dict) -> bool:
        with self.transaction() as db:
            return bool(db.execute(
                "INSERT OR IGNORE INTO candidates VALUES(?,?,?,?)",
                (candidate_id, symbol, emitted_at, _json(payload)),
            ).rowcount)

    def insert_execution_quote(self, quote) -> bool:
        from .models import utc_iso
        with self.transaction() as db:
            return bool(db.execute(
                """INSERT OR IGNORE INTO execution_quotes(
                event_id,symbol,event_time,received_at,update_id,best_bid,best_ask,source
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    quote.event_id, quote.symbol, utc_iso(quote.event_time),
                    utc_iso(quote.received_at), int(quote.update_id),
                    float(quote.best_bid), float(quote.best_ask), quote.source,
                ),
            ).rowcount)

    def insert_candle_open(self, event) -> bool:
        from .models import utc_iso
        with self.transaction() as db:
            return bool(db.execute(
                """INSERT OR IGNORE INTO candle_open_events(
                event_id,symbol,open_time,received_at,open_price,source
                ) VALUES(?,?,?,?,?,?)""",
                (
                    event.event_id, event.symbol, utc_iso(event.open_time),
                    utc_iso(event.received_at), float(event.open_price), event.source,
                ),
            ).rowcount)

    def latest_execution_quote(self, symbol: str, not_before: str | None = None):
        from datetime import datetime
        from .models import ExecutionQuote
        query = "SELECT * FROM execution_quotes WHERE symbol=?"
        values: list[Any] = [symbol]
        if not_before is not None:
            query += " AND julianday(event_time)>=julianday(?)"
            values.append(not_before)
        query += " ORDER BY julianday(event_time) DESC, update_id DESC LIMIT 1"
        row = self._connection.execute(query, values).fetchone()
        if row is None:
            return None
        parse = lambda value: datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return ExecutionQuote(
            symbol=row["symbol"], event_time=parse(row["event_time"]),
            received_at=parse(row["received_at"]), best_bid=float(row["best_bid"]),
            best_ask=float(row["best_ask"]), update_id=int(row["update_id"]),
            source=row["source"],
        )

    def first_execution_quote(self, symbol: str, not_before: str, not_after: str | None = None):
        from datetime import datetime
        from .models import ExecutionQuote
        query = (
            "SELECT * FROM execution_quotes "
            "WHERE symbol=? AND julianday(event_time)>=julianday(?)"
        )
        values: list[Any] = [symbol, not_before]
        if not_after is not None:
            query += " AND julianday(event_time)<=julianday(?)"
            values.append(not_after)
        query += " ORDER BY julianday(event_time), update_id LIMIT 1"
        row = self._connection.execute(query, values).fetchone()
        if row is None:
            return None
        parse = lambda value: datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return ExecutionQuote(
            symbol=row["symbol"], event_time=parse(row["event_time"]),
            received_at=parse(row["received_at"]), best_bid=float(row["best_bid"]),
            best_ask=float(row["best_ask"]), update_id=int(row["update_id"]),
            source=row["source"],
        )

    def execution_quotes_since(self, symbol: str, not_before: str):
        """Return persisted public quotes in deterministic exchange-event order."""
        from datetime import datetime
        from .models import ExecutionQuote

        with self._lock:
            rows = list(self._connection.execute(
                """
                SELECT *
                FROM execution_quotes
                WHERE symbol=? AND julianday(event_time)>=julianday(?)
                ORDER BY julianday(event_time), update_id
                """,
                (symbol, not_before),
            ))
        parse = lambda value: datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return [
            ExecutionQuote(
                symbol=row["symbol"], event_time=parse(row["event_time"]),
                received_at=parse(row["received_at"]), best_bid=float(row["best_bid"]),
                best_ask=float(row["best_ask"]), update_id=int(row["update_id"]),
                source=row["source"],
            )
            for row in rows
        ]

    def candle_open(self, symbol: str, open_time: str):
        from datetime import datetime
        from .models import CandleOpenEvent
        row = self._connection.execute(
            "SELECT * FROM candle_open_events WHERE symbol=? AND open_time=?",
            (symbol, open_time),
        ).fetchone()
        if row is None:
            return None
        parse = lambda value: datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return CandleOpenEvent(
            symbol=row["symbol"], open_time=parse(row["open_time"]),
            received_at=parse(row["received_at"]), open_price=float(row["open_price"]),
            source=row["source"],
        )

    def append_event(self, event: dict, dedupe_key: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO policy_events(
                event_id,dedupe_key,policy_id,candidate_id,thesis_id,stage,status,
                emitted_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    event["event_id"], dedupe_key, event["identity"]["policy_id"],
                    event["candidate_id"], event["thesis_id"], event["stage"],
                    event["status"], event["emitted_at_utc"], _json(event),
                ),
            )
            return bool(cursor.rowcount)

    def upsert_thesis(
        self, policy_id: str, thesis_id: str, candidate_id: str, symbol: str,
        status: str, state: dict, updated_at: str,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO theses VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(policy_id,thesis_id) DO UPDATE SET
                status=excluded.status,state_json=excluded.state_json,
                updated_at=excluded.updated_at""",
                (policy_id, thesis_id, candidate_id, symbol, status, _json(state), updated_at),
            )

    def thesis_for_candidate(self, policy_id: str, candidate_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM theses WHERE policy_id=? AND candidate_id=?",
            (policy_id, candidate_id),
        ).fetchone()
        return self._decode_state_row(row)

    def theses_for_candidate(self, candidate_id: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM theses WHERE candidate_id=? ORDER BY policy_id", (candidate_id,)
        )
        return [self._decode_state_row(row) for row in rows]

    def active_theses(self, policy_id: str | None = None) -> list[dict]:
        terminal = (
            "ENTRY_READY", "INVALIDATED", "BIAS_LOST", "EXPIRED", "DATA_UNAVAILABLE",
            "DATA_UNAVAILABLE_NO_EXECUTION_STREAM", "DATA_UNAVAILABLE_NO_FRESH_QUOTE_WITHIN_5S",
            "DATA_UNAVAILABLE_NO_PROSPECTIVE_1M_OPEN",
        )
        placeholders = ",".join("?" for _ in terminal)
        query = f"SELECT * FROM theses WHERE status NOT IN ({placeholders})"
        values: list[Any] = list(terminal)
        if policy_id:
            query += " AND policy_id=?"
            values.append(policy_id)
        return [self._decode_state_row(row) for row in self._connection.execute(query, values)]

    def upsert_position(self, state: dict) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(policy_id,position_id) DO UPDATE SET
                status=excluded.status,state_json=excluded.state_json,
                updated_at=excluded.updated_at""",
                (
                    state["policy_id"], state["position_id"], state["thesis_id"],
                    state["symbol"], state["status"], _json(state),
                    state["opened_at"], state["updated_at"],
                ),
            )

    def open_positions(self, policy_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM positions WHERE status='OPEN'"
        values: tuple[Any, ...] = ()
        if policy_id:
            query += " AND policy_id=?"
            values = (policy_id,)
        query += " ORDER BY opened_at, symbol"
        return [self._decode_state_row(row) for row in self._connection.execute(query, values)]

    def position_for_thesis(self, policy_id: str, thesis_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM positions WHERE policy_id=? AND thesis_id=?",
            (policy_id, thesis_id),
        ).fetchone()
        return self._decode_state_row(row)

    def events(self, policy_id: str | None = None) -> list[dict]:
        query = "SELECT payload_json FROM policy_events"
        values: tuple[Any, ...] = ()
        if policy_id:
            query += " WHERE policy_id=?"
            values = (policy_id,)
        query += " ORDER BY emitted_at,event_id"
        return [json.loads(row[0]) for row in self._connection.execute(query, values)]

    def snapshots(self) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self._connection.execute(
                "SELECT payload_json FROM market_snapshots ORDER BY as_of,snapshot_id"
            )
        ]

    def candidates(self) -> list[dict]:
        result = []
        for row in self._connection.execute(
            "SELECT candidate_id,symbol,emitted_at,payload_json FROM candidates ORDER BY emitted_at"
        ):
            result.append({
                "candidate_id": row["candidate_id"],
                "symbol": row["symbol"],
                "emitted_at": row["emitted_at"],
                "event": json.loads(row["payload_json"]),
            })
        return result

    def event_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM policy_events").fetchone()[0])

    def counts(self) -> dict[str, int]:
        return {
            table: int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "market_snapshots", "candidates", "policy_events", "theses", "positions",
                "execution_quotes", "candle_open_events",
            )
        }

    @staticmethod
    def _decode_state_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        state = json.loads(row["state_json"])
        for key in row.keys():
            if key != "state_json" and key not in state:
                state[key] = row[key]
        return state
