"""Causal, read-only Freeze R4 gate sidecar on Binance USD-M public data.

The selected environment is explicit and restricted to the approved Demo or
Mainnet public base URL.  The process has no credential input and no
exchange-write surface.  It stores one decision per (symbol, closed 5m
candle), while poll counters are kept separately so repeated 60-second
observations never inflate the research funnel.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shadow_runtime.market import (  # noqa: E402
    DEMO_USDM_PUBLIC_URL,
    PublicBinanceDemoMarket,
    detect_active_event as deployed_detect_active_event,
)
from strategy.active_altcoin_shadow import detect_active_event  # noqa: E402
from strategy.prospective_shadow_spec import (  # noqa: E402
    ACTIVE_CANDIDATE_CONTRACT,
    build_freeze_spec,
)


FREEZE_HASH = "62A4237F1178729BA9E6143D6B7C5FF3FCD7D74C3C7E710E681DA01FA6219D86"
DETECTOR_HASH = "20A7626F141DBE96DB6C79EAF12ABDAAAB47C423BACCC74F53E9835A05C4AFA3"
REGION = "europe-west4-drams3a"
MAINNET_USDM_PUBLIC_URL = "https://fapi.binance.com"
PUBLIC_MARKET_ENDPOINTS = {
    "demo": DEMO_USDM_PUBLIC_URL,
    "mainnet": MAINNET_USDM_PUBLIC_URL,
}
GATES = (
    "return_5m", "return_20m", "same_direction", "volume_expansion",
    "quote_volume_5m", "body_fraction", "directional_close", "atr_valid",
    "score_passed", "micro_gate_passed",
)
FUNNEL_NAMES = (
    "scanned_unique", "return_5m", "return_20m", "same_direction",
    "volume_expansion", "quote_volume", "body_fraction", "directional_close",
    "atr_valid", "score", "micro", "raw_LONG", "raw_SHORT",
    "B_LONG_after_top2", "B_LONG_after_cooldown",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest().upper()


def parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: Any) -> str:
    return parse_time(value).astimezone(timezone.utc).isoformat()


def closed_5m_boundary(server_time: datetime) -> tuple[datetime, datetime]:
    now = server_time.astimezone(timezone.utc)
    current_open = now.replace(second=0, microsecond=0, minute=(now.minute // 5) * 5)
    closed_open = current_open - timedelta(minutes=5)
    return closed_open, current_open - timedelta(milliseconds=1)


def validate_public_endpoint(
    market: PublicBinanceDemoMarket,
    expected_base_url: str,
) -> None:
    """Fail closed unless the exact, explicitly selected public URL is used."""
    approved = frozenset(PUBLIC_MARKET_ENDPOINTS.values())
    expected = str(expected_base_url).rstrip("/")
    actual = str(market.base_url).rstrip("/")
    if expected not in approved:
        raise RuntimeError(f"UNAPPROVED_PUBLIC_ENDPOINT:{expected}")
    if actual != expected:
        raise RuntimeError(f"PUBLIC_ENDPOINT_MISMATCH:{actual}!={expected}")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(payload) + "\n")
        handle.flush()


def gate_flags(event: dict[str, Any]) -> dict[str, bool]:
    base = event.get("gates") or {}
    return {
        "return_5m": bool(base.get("return_5m")),
        "return_20m": bool(base.get("return_20m")),
        "same_direction": bool(base.get("same_direction")),
        "volume_expansion": bool(base.get("volume_expansion")),
        "quote_volume_5m": bool(base.get("quote_volume_5m")),
        "body_fraction": bool(base.get("body_fraction")),
        "directional_close": bool(base.get("directional_close")),
        "atr_valid": bool(base.get("atr_valid")),
        "score_passed": bool(event.get("score_passed")),
        "micro_gate_passed": bool((event.get("micro") or {}).get("gate_passed")),
    }


def parity_projection(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "symbol": event.get("symbol"),
        "side": event.get("side"),
        "open_time": event.get("open_time"),
        "close_time": event.get("close_time"),
        "event_score": event.get("event_score"),
        "qualified": event.get("qualified"),
        "reason": event.get("reason"),
        "gates": gate_flags(event),
    }


class DiagnosticStore:
    """SQLite-backed unique-decision store and restart-safe counters."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS universes (
                boundary_open TEXT PRIMARY KEY, boundary_close TEXT NOT NULL,
                host_received_at TEXT NOT NULL, binance_server_time TEXT NOT NULL,
                clock_offset_ms REAL NOT NULL, symbols_json TEXT NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS decisions (
                decision_key TEXT PRIMARY KEY, universe_boundary TEXT NOT NULL,
                symbol TEXT NOT NULL, closed_5m_open_time TEXT NOT NULL,
                closed_5m_close_time TEXT NOT NULL, side TEXT, event_id TEXT,
                event_score REAL, qualified INTEGER NOT NULL,
                return_5m INTEGER NOT NULL, return_20m INTEGER NOT NULL,
                same_direction INTEGER NOT NULL, volume_expansion INTEGER NOT NULL,
                quote_volume_5m INTEGER NOT NULL, body_fraction INTEGER NOT NULL,
                directional_close INTEGER NOT NULL, atr_valid INTEGER NOT NULL,
                score_passed INTEGER NOT NULL, micro_gate_passed INTEGER NOT NULL,
                top2 INTEGER NOT NULL DEFAULT 0, cooldown_accepted INTEGER NOT NULL DEFAULT 0,
                input_hash TEXT NOT NULL, parity_checked INTEGER NOT NULL,
                parity_ok INTEGER, event_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS decisions_boundary_idx
                ON decisions(universe_boundary, symbol);
            CREATE TABLE IF NOT EXISTS polls (
                poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_received_at TEXT NOT NULL, binance_server_time TEXT NOT NULL,
                boundary_open TEXT NOT NULL, symbols_requested INTEGER NOT NULL,
                api_errors INTEGER NOT NULL, unique_inserted INTEGER NOT NULL,
                parity_checked INTEGER NOT NULL, parity_mismatches INTEGER NOT NULL
            );
        """)
        self.db.commit()

    def ensure_universe(
        self,
        boundary_open: str,
        boundary_close: str,
        host_received_at: str,
        server_time: str,
        offset_ms: float,
        symbols: list[str],
    ) -> list[str]:
        row = self.db.execute(
            "SELECT symbols_json FROM universes WHERE boundary_open=?", (boundary_open,)
        ).fetchone()
        if row:
            return list(json.loads(row["symbols_json"]))
        self.db.execute(
            "INSERT INTO universes VALUES (?,?,?,?,?,?,0)",
            (boundary_open, boundary_close, host_received_at, server_time, offset_ms, canonical_json(symbols)),
        )
        self.db.commit()
        return symbols

    def add_decision(self, row: dict[str, Any]) -> bool:
        flags = row["gates"]
        cursor = self.db.execute("""
            INSERT OR IGNORE INTO decisions (
                decision_key, universe_boundary, symbol, closed_5m_open_time,
                closed_5m_close_time, side, event_id, event_score, qualified,
                return_5m, return_20m, same_direction, volume_expansion,
                quote_volume_5m, body_fraction, directional_close, atr_valid,
                score_passed, micro_gate_passed, input_hash, parity_checked,
                parity_ok, event_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row["decision_key"], row["universe_boundary"], row["symbol"],
            row["closed_5m_open_time"], row["closed_5m_close_time"],
            row.get("side"), row.get("event_id"), row.get("event_score"),
            int(bool(row.get("qualified"))),
            *[int(flags[name]) for name in GATES],
            row["input_hash"], int(row["parity_checked"]),
            None if row.get("parity_ok") is None else int(row["parity_ok"]),
            canonical_json(row["event"]),
        ))
        self.db.commit()
        return cursor.rowcount == 1

    def finalize_boundary(self, boundary_open: str, expected: int) -> bool:
        universe = self.db.execute(
            "SELECT complete FROM universes WHERE boundary_open=?", (boundary_open,)
        ).fetchone()
        if not universe or universe["complete"]:
            return bool(universe and universe["complete"])
        count = self.db.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE universe_boundary=?", (boundary_open,)
        ).fetchone()["n"]
        if count < expected:
            return False
        longs = self.db.execute("""
            SELECT decision_key, symbol, event_score FROM decisions
            WHERE universe_boundary=? AND qualified=1 AND side='LONG'
            ORDER BY event_score DESC, symbol ASC LIMIT 2
        """, (boundary_open,)).fetchall()
        boundary_time = parse_time(boundary_open)
        for candidate in longs:
            self.db.execute("UPDATE decisions SET top2=1 WHERE decision_key=?", (candidate["decision_key"],))
            previous = self.db.execute("""
                SELECT closed_5m_open_time FROM decisions
                WHERE symbol=? AND cooldown_accepted=1 AND universe_boundary<?
                ORDER BY universe_boundary DESC LIMIT 1
            """, (candidate["symbol"], boundary_open)).fetchone()
            cooldown_ok = not previous or (
                boundary_time - parse_time(previous["closed_5m_open_time"])
            ) >= timedelta(minutes=int(ACTIVE_CANDIDATE_CONTRACT["symbol_cooldown_minutes"]))
            if cooldown_ok:
                self.db.execute(
                    "UPDATE decisions SET cooldown_accepted=1 WHERE decision_key=?",
                    (candidate["decision_key"],),
                )
        self.db.execute("UPDATE universes SET complete=1 WHERE boundary_open=?", (boundary_open,))
        self.db.commit()
        return True

    def add_poll(self, row: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO polls(host_received_at,binance_server_time,boundary_open,symbols_requested,api_errors,unique_inserted,parity_checked,parity_mismatches) VALUES (?,?,?,?,?,?,?,?)",
            tuple(row[name] for name in (
                "host_received_at", "binance_server_time", "boundary_open",
                "symbols_requested", "api_errors", "unique_inserted",
                "parity_checked", "parity_mismatches",
            )),
        )
        self.db.commit()

    def summary(self) -> dict[str, Any]:
        query = self.db.execute("""
            SELECT COUNT(*) scanned_unique,
              SUM(return_5m) return_5m, SUM(return_5m*return_20m) return_20m,
              SUM(return_5m*return_20m*same_direction) same_direction,
              SUM(return_5m*return_20m*same_direction*volume_expansion) volume_expansion,
              SUM(return_5m*return_20m*same_direction*volume_expansion*quote_volume_5m) quote_volume,
              SUM(return_5m*return_20m*same_direction*volume_expansion*quote_volume_5m*body_fraction) body_fraction,
              SUM(return_5m*return_20m*same_direction*volume_expansion*quote_volume_5m*body_fraction*directional_close) directional_close,
              SUM(return_5m*return_20m*same_direction*volume_expansion*quote_volume_5m*body_fraction*directional_close*atr_valid) atr_valid,
              SUM(return_5m*return_20m*same_direction*volume_expansion*quote_volume_5m*body_fraction*directional_close*atr_valid*score_passed) score,
              SUM(qualified) micro,
              SUM(CASE WHEN qualified=1 AND side='LONG' THEN 1 ELSE 0 END) raw_LONG,
              SUM(CASE WHEN qualified=1 AND side='SHORT' THEN 1 ELSE 0 END) raw_SHORT,
              SUM(top2) B_LONG_after_top2, SUM(cooldown_accepted) B_LONG_after_cooldown,
              SUM(parity_checked) parity_checked,
              SUM(CASE WHEN parity_checked=1 AND parity_ok=0 THEN 1 ELSE 0 END) parity_mismatches
            FROM decisions
        """).fetchone()
        polls = self.db.execute("""
            SELECT COUNT(*) polls, COALESCE(SUM(symbols_requested),0) symbols_polled,
              COALESCE(SUM(api_errors),0) api_errors,
              COALESCE(SUM(unique_inserted),0) unique_inserted
            FROM polls
        """).fetchone()
        latest_universe = self.db.execute(
            "SELECT * FROM universes ORDER BY boundary_open DESC LIMIT 1"
        ).fetchone()
        events = self.db.execute("""
            SELECT event_id,symbol,side,closed_5m_open_time,event_score,top2,cooldown_accepted
            FROM decisions WHERE qualified=1 ORDER BY closed_5m_open_time DESC LIMIT 20
        """).fetchall()
        funnel = {name: int(query[name] or 0) for name in FUNNEL_NAMES}
        return {
            "unique_funnel": funnel,
            "operational": {name: int(polls[name] or 0) for name in polls.keys()},
            "parity": {
                "checked": int(query["parity_checked"] or 0),
                "mismatches": int(query["parity_mismatches"] or 0),
            },
            "latest_universe": dict(latest_universe) if latest_universe else None,
            "recent_unique_events": [dict(row) for row in events],
        }


def scan_once(
    market: PublicBinanceDemoMarket,
    store: DiagnosticStore,
    output: Path,
    as_of: datetime | None = None,
    expected_base_url: str = DEMO_USDM_PUBLIC_URL,
) -> dict[str, Any]:
    validate_public_endpoint(market, expected_base_url)
    host_received_at = utc_now()
    server_time = as_of or market.server_now()
    boundary_open, boundary_close = closed_5m_boundary(server_time)
    boundary_key = boundary_open.isoformat()
    discovered = market.liquid_symbols(ACTIVE_CANDIDATE_CONTRACT["candidate_limit_per_poll"])
    symbols = store.ensure_universe(
        boundary_key, boundary_close.isoformat(), host_received_at.isoformat(),
        server_time.isoformat(), (host_received_at - server_time).total_seconds() * 1000.0,
        discovered,
    )
    first_failure = Counter()
    api_errors = unique_inserted = parity_checked = parity_mismatches = 0
    unique_events: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols):
        try:
            payload = market.raw_klines(symbol, "1m", 180)
            frame = market._one_minute_frame(payload)
            event = detect_active_event(symbol, frame, server_time)
        except Exception as exc:
            api_errors += 1
            first_failure[f"API:{type(exc).__name__}"] += 1
            continue
        if event.get("reason") in {"INSUFFICIENT_HISTORY", "GAPPED_EVENT_HISTORY"}:
            first_failure[str(event.get("reason"))] += 1
            continue
        event_open = iso(event["open_time"])
        if event_open != boundary_key:
            first_failure["NOT_EXPECTED_CLOSED_5M"] += 1
            continue
        flags = gate_flags(event)
        alive = True
        for gate in GATES:
            alive = alive and flags[gate]
            if not alive:
                first_failure[f"gate:{gate}"] += 1
                break
        input_payload = {
            "symbol": symbol, "closed_5m_open_time": boundary_key,
            "closed_5m_close_time": boundary_close.isoformat(), "raw_1m": payload,
        }
        checked = index < 5
        parity_ok: bool | None = None
        if checked:
            parity_checked += 1
            deployed_event = deployed_detect_active_event(symbol, frame.copy(), server_time)
            parity_ok = parity_projection(event) == parity_projection(deployed_event)
            parity_mismatches += int(not parity_ok)
        row = {
            "decision_key": f"{symbol}|{boundary_key}",
            "universe_boundary": boundary_key, "symbol": symbol,
            "closed_5m_open_time": event_open,
            "closed_5m_close_time": iso(event["close_time"]),
            "side": event.get("side"), "event_id": event.get("event_id"),
            "event_score": event.get("event_score"), "qualified": bool(event.get("qualified")),
            "gates": flags, "input_hash": sha256_json(input_payload),
            "parity_checked": checked, "parity_ok": parity_ok, "event": event,
        }
        if store.add_decision(row):
            unique_inserted += 1
            if row["qualified"]:
                event_row = {
                    "unique_event_key": f"{symbol}|{boundary_key}|{row['side']}",
                    "event_id": row["event_id"], "symbol": symbol, "side": row["side"],
                    "closed_5m_open_time": boundary_key,
                    "closed_5m_close_time": row["closed_5m_close_time"],
                    "event_score": row["event_score"], "input_hash": row["input_hash"],
                }
                append_jsonl(output / "unique_events.jsonl", event_row)
                unique_events.append(event_row)
    complete = store.finalize_boundary(boundary_key, len(symbols))
    poll = {
        "host_received_at": host_received_at.isoformat(),
        "binance_server_time": server_time.isoformat(),
        "clock_offset_ms": (host_received_at - server_time).total_seconds() * 1000.0,
        "boundary_open": boundary_key, "boundary_close": boundary_close.isoformat(),
        "symbols_requested": len(symbols), "api_errors": api_errors,
        "unique_inserted": unique_inserted, "parity_checked": parity_checked,
        "parity_mismatches": parity_mismatches, "boundary_complete": complete,
        "first_failure": dict(first_failure), "new_unique_events": unique_events,
    }
    store.add_poll(poll)
    return poll


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--market-environment",
        choices=tuple(PUBLIC_MARKET_ENDPOINTS),
        default="demo",
    )
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if build_freeze_spec(ROOT)["freeze_hash"] != FREEZE_HASH:
        raise RuntimeError("FREEZE_IDENTITY_MISMATCH")
    if args.interval_seconds < 30:
        raise ValueError("interval must be at least 30 seconds")
    endpoint = PUBLIC_MARKET_ENDPOINTS[args.market_environment]
    requested_output = args.output_dir or Path(f"/data/r4_{args.market_environment}_gate_sidecar")
    output = requested_output if requested_output.is_absolute() else ROOT / requested_output
    output.mkdir(parents=True, exist_ok=True)
    store = DiagnosticStore(output / "r4_gate_sidecar.sqlite3")
    market = PublicBinanceDemoMarket(base_url=endpoint)
    validate_public_endpoint(market, endpoint)
    started = utc_now()
    print(canonical_json({
        "event": "R4_GATE_SIDECAR_STARTUP", "started_at": started.isoformat(),
        "argv": sys.argv, "endpoint": endpoint,
        "market_environment": args.market_environment, "region": REGION,
        "read_only": True, "order_route": False, "credential_inputs": False,
        "freeze_hash": FREEZE_HASH, "detector_hash": DETECTOR_HASH,
        "db": str(store.path), "events": str(output / "unique_events.jsonl"),
        "summary": str(output / "summary.json"),
    }), flush=True)
    while True:
        fatal_error = None
        try:
            poll = scan_once(market, store, output, expected_base_url=endpoint)
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            poll = {
                "host_received_at": utc_now().isoformat(), "fatal_scan_error": fatal_error,
                "endpoint": endpoint, "market_environment": args.market_environment,
            }
        append_jsonl(output / "gate_events.jsonl", poll)
        stored = store.summary()
        summary = {
            "started_at": started.isoformat(), "updated_at": utc_now().isoformat(),
            "endpoint": endpoint, "market_environment": args.market_environment,
            "region": REGION,
            "read_only": True, "order_route": False,
            "freeze_hash": FREEZE_HASH, "detector_hash": DETECTOR_HASH,
            **stored, "last_poll": poll,
            "files": {
                "sqlite": str(store.path), "events": str(output / "unique_events.jsonl"),
                "polls": str(output / "gate_events.jsonl"),
                "summary": str(output / "summary.json"),
            },
        }
        atomic_json(output / "summary.json", summary)
        print(canonical_json({
            "event": "R4_GATE_SIDECAR_HEARTBEAT", "updated_at": summary["updated_at"],
            "unique_funnel": summary["unique_funnel"], "operational": summary["operational"],
            "parity": summary["parity"], "last_poll": poll,
        }), flush=True)
        if args.once:
            return 0 if not fatal_error else 2
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
