"""Invalidated pre-R4 runner retained only for forensic reproducibility."""

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
from shadow_runtime.identity import runtime_identity
from shadow_runtime.market import PublicBinanceDemoMarket
from shadow_runtime.models import Candidate
from shadow_runtime.store import ShadowStore


def _write_artifacts(store: ShadowStore, output: Path, identity: dict, mode: str) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    events = store.events()
    with (output / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "mode": mode,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime_identity": identity,
        "counts": store.counts(),
        "by_policy": {},
    }
    policy_ids = sorted({event["identity"]["policy_id"] for event in events})
    for policy_id in policy_ids:
        rows = [event for event in events if event["identity"]["policy_id"] == policy_id]
        summary["by_policy"][policy_id] = {
            "events": len(rows),
            "candidates": sum(row["stage"] == "CANDIDATE_EMITTED" for row in rows),
            "entry_ready": sum(row["stage"] == "ENTRY_READY" for row in rows),
            "simulated_fills": sum(row["stage"] == "SIMULATED_FILL" for row in rows),
            "terminal_outcomes": sum(row["stage"] == "TERMINAL_OUTCOME" for row in rows),
            "net_R": sum(float((row.get("outcome") or {}).get("net_R") or 0) for row in rows),
        }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def dry_run(root: Path, database: Path, output: Path) -> dict:
    store = ShadowStore(database)
    try:
        runtime = ShadowRuntime(root=root, store=store)
        first = synthetic_snapshot()
        runtime.process_snapshot(first)
        # Exact replay proves idempotency: no duplicated event/thesis/fill.
        runtime.process_snapshot(first)
        return _write_artifacts(store, output, runtime.identity, "OFFLINE_DRY_RUN")
    finally:
        store.close()


def demo_once(root: Path, database: Path, output: Path) -> dict:
    market = PublicBinanceDemoMarket()
    store = ShadowStore(database)
    try:
        runtime = ShadowRuntime(root=root, store=store)
        filters = market.exchange_filters()
        for candidate, cached_1m in market.discover_candidates():
            snapshot = market.snapshot(candidate, cached_1m=cached_1m)
            runtime.process_snapshot(snapshot, exchange_filter=filters.get(candidate.symbol))
        return _write_artifacts(store, output, runtime.identity, "BINANCE_DEMO_PUBLIC_ONCE")
    finally:
        store.close()


def demo_loop(root: Path, database: Path, output: Path, *, duration_seconds: int, poll_seconds: int) -> dict:
    market = PublicBinanceDemoMarket()
    store = ShadowStore(database)
    try:
        runtime = ShadowRuntime(root=root, store=store)
        filters = market.exchange_filters()
        stored_candidates = store.candidates()
        known_ids = {row["candidate_id"] for row in stored_candidates}
        last_symbol_event = {
            row["symbol"]: datetime.fromisoformat(row["emitted_at"].replace("Z", "+00:00"))
            for row in stored_candidates
        }
        active = {}
        for row in stored_candidates:
            if runtime.candidate_complete(row["candidate_id"]):
                continue
            active[row["candidate_id"]] = Candidate(
                candidate_id=row["candidate_id"], symbol=row["symbol"], side="LONG",
                emitted_at=datetime.fromisoformat(row["emitted_at"].replace("Z", "+00:00")),
                event=row["event"],
            )
        started = time.monotonic()
        first_cycle = True
        while first_cycle or time.monotonic() - started < duration_seconds:
            first_cycle = False
            now = market.server_now()
            discovered = market.discover_candidates(as_of=now)
            cached = {}
            for candidate, cached_1m in discovered:
                if candidate.candidate_id in known_ids and candidate.candidate_id not in active:
                    continue
                previous = last_symbol_event.get(candidate.symbol)
                if (
                    candidate.candidate_id not in known_ids
                    and previous is not None
                    and candidate.emitted_at - previous < timedelta(minutes=60)
                ):
                    continue
                active[candidate.candidate_id] = candidate
                cached[candidate.candidate_id] = cached_1m
                known_ids.add(candidate.candidate_id)
                last_symbol_event[candidate.symbol] = candidate.emitted_at
            for candidate in list(active.values()):
                snapshot = market.snapshot(candidate, cached_1m=cached.get(candidate.candidate_id))
                runtime.process_snapshot(snapshot, exchange_filter=filters.get(candidate.symbol))
                if runtime.candidate_complete(candidate.candidate_id):
                    active.pop(candidate.candidate_id, None)
            # A machine-readable heartbeat is rewritten after every complete
            # cycle, so a long-running process can be audited without stopping.
            _write_artifacts(store, output, runtime.identity, "BINANCE_DEMO_PUBLIC_LOOP")
            if time.monotonic() - started >= duration_seconds:
                break
            time.sleep(max(1, min(int(poll_seconds), 30)))
        return _write_artifacts(store, output, runtime.identity, "BINANCE_DEMO_PUBLIC_LOOP")
    finally:
        store.close()


def main() -> int:
    raise SystemExit(
        "Freeze R3 runtime is invalidated: retroactive A fills and sampled exits. "
        "Use scripts/run_prospective_shadow_r4.py after independent R4 review."
    )
    root_default = ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "demo-once", "demo-loop"), default="dry-run")
    parser.add_argument("--root", default=str(root_default))
    parser.add_argument("--database")
    parser.add_argument("--output")
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    base = root / "research_data" / "shadow_runtime_r3"
    database = Path(args.database) if args.database else base / f"{args.mode}.sqlite3"
    output = Path(args.output) if args.output else base / args.mode
    if args.mode == "dry-run":
        result = dry_run(root, database, output)
    elif args.mode == "demo-once":
        result = demo_once(root, database, output)
    else:
        result = demo_loop(
            root, database, output,
            duration_seconds=max(1, args.duration_seconds),
            poll_seconds=max(1, args.poll_seconds),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
