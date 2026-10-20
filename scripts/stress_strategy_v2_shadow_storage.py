"""Synthetic bounded-storage stress run for the Shadow-only evidence store."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategy.strategy_v2_shadow_storage import ShadowEvidenceStore


def run(output_dir: str, *, decisions: int = 500,
        management_events: int = 50_000, outcomes: int = 500) -> dict:
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    store = ShadowEvidenceStore(output_dir)
    peak = 0
    started = time.perf_counter()
    for index in range(decisions):
        proposal_id = f"proposal-{index:05d}"
        payload = {
            "execution_authority": False,
            "proposal_id": proposal_id,
            "decision_timestamp": f"2026-10-{1 + index // 1440:02d}T12:00:00Z",
            "symbol": f"SYM{index % 100}/USDT",
            "decision_record_hash": f"{index:064X}",
            "reentry_reset_evidence": [{
                "event": "NEW_BOS", "timestamp": "2026-10-20T11:00:00Z",
                "details": "x" * 256,
            }],
            "payload": "d" * 1024,
        }
        store.save_strategy_v2_shadow_decision(payload)
        store.record_closed_thesis(
            payload["symbol"], f"thesis-{index}", side="BUY",
            closed_at="2026-10-20T12:30:00Z", details={"payload": "t" * 256},
        )
    for index in range(management_events):
        store.append_strategy_v2_management_shadow(
            f"trade-{index % 500}",
            {
                "execution_authority": False,
                "timestamp": f"2026-10-20T12:{index % 60:02d}:{index % 60:02d}Z",
                "structure_confirmed_at": "2026-10-20T11:45:00Z",
                "candidate_stop": 100.0 + index / 100_000.0,
                "reason": "SYNTHETIC_STRESS",
                "evidence": "m" * 1024,
            },
        )
        if index % 1000 == 0:
            peak = max(peak, store.stats()["disk_bytes"])
    for index in range(outcomes):
        proposal_id = f"proposal-{index:05d}"
        store.record_strategy_v2_shadow_outcome(
            proposal_id,
            {
                "proposal_id": proposal_id, "trade_id": f"trade-{index}",
                "recorded_at": "2026-10-20T13:00:00Z",
                "execution_authority": False, "realized_R": index / 100.0,
                "payload": "o" * 512,
            },
        )
    elapsed = time.perf_counter() - started
    final = store.stats()
    peak = max(peak, final["disk_bytes"])
    restarted_at = time.perf_counter()
    recovered = ShadowEvidenceStore(output_dir).stats()
    recovery_seconds = time.perf_counter() - restarted_at
    return {
        "input": {
            "decisions": decisions, "management_events": management_events,
            "outcomes": outcomes,
        },
        "final": final,
        "peak_disk_bytes_observed": peak,
        "elapsed_seconds": elapsed,
        "recovery_seconds": recovery_seconds,
        "recovered_quick_check": recovered["quick_check"],
        "write_count": final["metrics"].get("write_transactions", 0),
        "approx_payload_bytes": final["metrics"].get("approx_payload_bytes", 0),
        "pruned_records": final["metrics"].get("records_pruned", 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir")
    parser.add_argument("--result-json")
    args = parser.parse_args()
    temp = None
    output_dir = args.output_dir
    if not output_dir:
        temp = tempfile.TemporaryDirectory()
        output_dir = temp.name
    result = run(output_dir)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.result_json:
        with open(args.result_json, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    if temp:
        temp.cleanup()


if __name__ == "__main__":
    main()
