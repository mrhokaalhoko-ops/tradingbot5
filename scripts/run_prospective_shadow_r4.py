"""Run the causal R4 experiment in Shadow-only mode (never sends orders)."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shadow_runtime.engine import ShadowRuntime
from shadow_runtime.fixtures import synthetic_snapshot
from shadow_runtime.market import PublicBinanceDemoMarket
from shadow_runtime.models import Candidate, ExecutionQuote
from shadow_runtime.store import ShadowStore
from shadow_runtime.stream import PublicMarketStream, StreamExecutionProvider


def _heartbeat(store: ShadowStore) -> dict:
    return store.get_meta("heartbeat", {
        "cycles_completed": 0, "symbols_scanned": 0, "raw_active_events": 0,
        "qualified_candidates": 0, "accepted_after_cooldown": 0,
        "api_errors": 0, "scanner_latency_ms": [], "quote_events": 0,
        "candle_open_events": 0, "last_successful_cycle_at": None,
    })


def _write_artifacts(store: ShadowStore, output: Path, identity: dict, mode: str) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    events = store.events()
    with (output / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    heartbeat = _heartbeat(store)
    latencies = list(heartbeat.get("scanner_latency_ms") or [])
    summary = {
        "mode": mode,
        "valid_sample": mode != "PRE_R4_EXCLUDED",
        "control_scope": "CONDITIONAL_ON_SHARED_ACTIVE_CANDIDATES_NOT_FULL_PRODUCTION",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime_identity": identity, "counts": store.counts(), "heartbeat": heartbeat,
        "scanner_latency_ms": {
            "last": latencies[-1] if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": sum(latencies) / len(latencies) if latencies else None,
        },
        "by_policy": {},
    }
    for policy_id in sorted({event["identity"]["policy_id"] for event in events}):
        rows = [event for event in events if event["identity"]["policy_id"] == policy_id]
        summary["by_policy"][policy_id] = {
            "events": len(rows),
            "candidates": sum(row["stage"] == "CANDIDATE_EMITTED" for row in rows),
            "entry_ready": sum(row["stage"] == "ENTRY_READY" for row in rows),
            "simulated_fills": sum(row["stage"] == "SIMULATED_FILL" for row in rows),
            "terminal_outcomes": sum(row["stage"] == "TERMINAL_OUTCOME" for row in rows),
            "ambiguous": sum(
                bool((row.get("outcome") or {}).get("same_bar_conflict")) for row in rows
            ),
            "net_R": sum(float((row.get("outcome") or {}).get("net_R") or 0) for row in rows),
        }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


class _DryProvider:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.quote = ExecutionQuote(
            symbol=snapshot.symbol, event_time=snapshot.as_of,
            received_at=snapshot.as_of, best_bid=snapshot.quote.best_bid,
            best_ask=snapshot.quote.best_ask, update_id=int(snapshot.as_of.timestamp() * 1000),
            source="OFFLINE_FAKE_EXCHANGE_PUBLIC_QUOTE",
        )

    def wait_fresh_quote(self, symbol, ready_at, timeout_seconds=5.0):
        return self.quote

    def first_quote_near(self, symbol, ready_at, window_seconds=2.0):
        return self.quote if self.quote.event_time >= ready_at else None

    def exact_candle_open(self, symbol, open_time):
        return None

    def wait_candle_open(self, symbol, open_time, timeout_seconds=5.0):
        return None


def dry_run(root: Path, database: Path, output: Path) -> dict:
    store = ShadowStore(database)
    try:
        runtime = ShadowRuntime(root=root, store=store)
        first = synthetic_snapshot()
        provider = _DryProvider(first)
        runtime.process_snapshot(first, execution_provider=provider)
        runtime.process_snapshot(first, execution_provider=provider)
        return _write_artifacts(store, output, runtime.identity, "OFFLINE_CAUSAL_DRY_RUN")
    finally:
        store.close()


def _restore_candidates(store: ShadowStore, runtime: ShadowRuntime):
    active = {}
    last_symbol_event = {}
    known = set()
    for row in store.candidates():
        known.add(row["candidate_id"])
        emitted = datetime.fromisoformat(row["emitted_at"].replace("Z", "+00:00"))
        last_symbol_event[row["symbol"]] = emitted
        if not runtime.candidate_complete(row["candidate_id"]):
            active[row["candidate_id"]] = Candidate(
                candidate_id=row["candidate_id"], symbol=row["symbol"], side="LONG",
                emitted_at=emitted, event=row["event"],
            )
    return active, known, last_symbol_event


def demo_loop(root: Path, database: Path, output: Path, *, duration_seconds: int, poll_seconds: int) -> dict:
    market = PublicBinanceDemoMarket()
    store = ShadowStore(database)
    stream = PublicMarketStream(())
    stream.start()
    provider = StreamExecutionProvider(store, stream)
    try:
        runtime = ShadowRuntime(root=root, store=store)
        filters = market.exchange_filters()
        active, known_ids, last_symbol_event = _restore_candidates(store, runtime)
        stream.update_symbols(candidate.symbol for candidate in active.values())
        heartbeat = _heartbeat(store)
        def drain_stream_events():
            for item in provider.drain_for_runtime():
                if isinstance(item, ExecutionQuote):
                    runtime.process_execution_quote(item)
                    heartbeat["quote_events"] += 1
                else:
                    heartbeat["candle_open_events"] += 1
        started = time.monotonic()
        next_scan = started
        while time.monotonic() - started < duration_seconds:
            drain_stream_events()

            now_mono = time.monotonic()
            if now_mono >= next_scan:
                try:
                    now = market.server_now()
                    scan = market.scan_candidates(as_of=now)
                    heartbeat["cycles_completed"] += 1
                    heartbeat["symbols_scanned"] += scan.symbols_scanned
                    heartbeat["raw_active_events"] += scan.raw_active_events
                    heartbeat["qualified_candidates"] += scan.qualified_candidates
                    heartbeat["api_errors"] += scan.api_errors
                    heartbeat["scanner_latency_ms"] = (
                        list(heartbeat.get("scanner_latency_ms") or []) + [scan.latency_ms]
                    )[-1000:]
                    cached = {}
                    for candidate, cached_1m in scan.candidates:
                        if candidate.candidate_id in known_ids and candidate.candidate_id not in active:
                            continue
                        previous = last_symbol_event.get(candidate.symbol)
                        if candidate.candidate_id not in known_ids and previous is not None and candidate.emitted_at - previous < timedelta(minutes=60):
                            continue
                        active[candidate.candidate_id] = candidate
                        cached[candidate.candidate_id] = cached_1m
                        known_ids.add(candidate.candidate_id)
                        last_symbol_event[candidate.symbol] = candidate.emitted_at
                        heartbeat["accepted_after_cooldown"] += 1
                    stream.update_symbols(candidate.symbol for candidate in active.values())
                    for candidate in list(active.values()):
                        snapshot = market.snapshot(candidate, cached_1m=cached.get(candidate.candidate_id))
                        one_minute = snapshot.closed("1m")
                        if one_minute:
                            bar = one_minute[-1]
                            key = f"last_reconciled_1m:{candidate.symbol}"
                            if store.get_meta(key) != bar.candle_id:
                                runtime.reconcile_closed_bar(
                                    candidate.symbol, bar,
                                    ordered_quote_coverage=stream.has_continuous_coverage(
                                        candidate.symbol, bar.open_time, bar.close_time
                                    ),
                                )
                                store.put_meta(key, bar.candle_id)
                        runtime.process_snapshot(
                            snapshot, exchange_filter=filters.get(candidate.symbol),
                            execution_provider=provider,
                        )
                        if runtime.candidate_complete(candidate.candidate_id):
                            active.pop(candidate.candidate_id, None)
                    stream.update_symbols(candidate.symbol for candidate in active.values())
                    # The REST scan may take seconds; the background websocket
                    # kept capturing. Replay that ordered queue before writing
                    # the cycle heartbeat or checking the duration boundary.
                    drain_stream_events()
                    heartbeat["last_successful_cycle_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                except Exception as exc:
                    heartbeat["api_errors"] += 1
                    heartbeat["last_error"] = f"{type(exc).__name__}:{exc}"
                heartbeat.update({
                    "stream_errors": stream.errors, "stream_reconnects": stream.reconnects,
                    "active_theses": len(store.active_theses()),
                    "open_positions": len(store.open_positions()),
                })
                store.put_meta("heartbeat", heartbeat)
                _write_artifacts(store, output, runtime.identity, "BINANCE_DEMO_PUBLIC_CAUSAL_LOOP")
                next_scan += float(poll_seconds)
                if next_scan <= time.monotonic():
                    next_scan = time.monotonic() + float(poll_seconds)
            time.sleep(0.05)
        drain_stream_events()
        heartbeat.update({
            "stream_errors": stream.errors, "stream_reconnects": stream.reconnects,
            "active_theses": len(store.active_theses()),
            "open_positions": len(store.open_positions()),
        })
        store.put_meta("heartbeat", heartbeat)
        return _write_artifacts(store, output, runtime.identity, "BINANCE_DEMO_PUBLIC_CAUSAL_LOOP")
    finally:
        stream.stop()
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "demo-loop"), default="dry-run")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--database")
    parser.add_argument("--output")
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    base = root / "research_data" / "shadow_runtime_r4"
    database = Path(args.database) if args.database else base / f"{args.mode}.sqlite3"
    output = Path(args.output) if args.output else base / args.mode
    if args.mode == "dry-run":
        result = dry_run(root, database, output)
    else:
        result = demo_loop(
            root, database, output, duration_seconds=max(1, args.duration_seconds),
            poll_seconds=max(1, args.poll_seconds),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
