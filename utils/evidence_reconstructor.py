"""Deterministic reconstruction checks over exported decision-audit events."""

from __future__ import annotations

import json
import zipfile
from collections import defaultdict


DRILLS = {
    "A_QUALITY_REJECTION": [
        ("MARKET_CONTEXT", None), ("QUALITY_FILTER", "REJECTED"),
        ("FINAL_OUTCOME", None),
    ],
    "B_CONFIRMATION_WAIT": [
        ("ENTRY_ZONE_TOUCH", "TOUCHED"), ("ENTRY_CONFIRMATION", "WAIT"),
    ],
    "C_VIRTUAL_INVALIDATION": [
        ("INVALIDATION_STATE", "SWEEP_PENDING"),
        ("INVALIDATION_STATE", "HARD_INVALIDATED"), ("SNIPER", None),
    ],
    "D_CONFIRMATION_TO_EXECUTION": [
        ("ENTRY_CONFIRMATION", "PASS"),
        ("EXECUTION_TIMELINE", "ORDER_INTENT_PERSISTED"),
        ("EXECUTION_TIMELINE", "ENTRY_ORDER_ACKNOWLEDGED"),
        ("EXECUTION_TIMELINE", "PROTECTION_RESULT"),
        ("EXECUTION", "EXECUTED"),
    ],
    "E_TRAILING_TO_CLOSE": [
        ("TRAILING_STOP", "ACTIVATED"),
        ("TRAILING_STOP", "SL_REPLACED"), ("POSITION_CLOSED", None),
    ],
    "F_MANUAL_CLOSE": [("POSITION_CLOSED", "MANUAL_CLOSE")],
    "G_RESTART_RECOVERY": [
        ("RUN_SESSION", "STARTED"), ("STARTUP_RECOVERY", "RECOVERY_COMPLETE"),
    ],
    "H_WS_STALE_REST_FALLBACK": [
        ("RESOURCE_SNAPSHOT", "WEBSOCKET_DISCONNECT"),
        ("PRICE_SOURCE", "REST_FALLBACK"),
    ],
}


def iter_audit_events(paths):
    seen = set()
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.startswith("decision_audit_") or not name.endswith(".jsonl"):
                    continue
                for raw in archive.read(name).decode("utf-8", errors="replace").splitlines():
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    event_id = event.get("event_id")
                    if event_id and event_id in seen:
                        continue
                    if event_id:
                        seen.add(event_id)
                    yield event


def _identity(event):
    return next((
        str(event.get(key)) for key in (
            "trade_id", "position_id", "virtual_order_id", "setup_id",
            "thesis_id", "decision_id", "scan_id", "run_id",
        ) if event.get(key)
    ), "UNLINKED")


def _matches(event, requirement):
    stage, status = requirement
    if str(event.get("stage")) != stage:
        return False
    return status is None or str(event.get("status")).upper() == status


def reconstruct_drills(events):
    events = sorted(list(events), key=lambda event: (
        str(event.get("timestamp") or ""), int(event.get("event_sequence") or 0)
    ))
    groups = defaultdict(list)
    runs = defaultdict(list)
    for event in events:
        linked = False
        for key in (
            "trade_id", "position_id", "virtual_order_id", "setup_id",
            "thesis_id", "decision_id", "scan_id",
        ):
            if event.get(key):
                groups[f"{key}:{event[key]}"].append(event)
                linked = True
        if not linked:
            groups["UNLINKED"].append(event)
        if event.get("run_id"):
            runs[str(event["run_id"])].append(event)

    report = {}
    for name, requirements in DRILLS.items():
        candidates = runs.values() if name.startswith(("G_", "H_")) else groups.values()
        best = None
        for timeline in candidates:
            matched = []
            cursor = 0
            for event in timeline:
                if cursor < len(requirements) and _matches(event, requirements[cursor]):
                    matched.append(event)
                    cursor += 1
            candidate = {
                "status": "RECONSTRUCTABLE" if cursor == len(requirements) else "MISSING_EVIDENCE",
                "matched": [
                    {"timestamp": event.get("timestamp"), "stage": event.get("stage"),
                     "status": event.get("status"), "event_id": event.get("event_id")}
                    for event in matched
                ],
                "missing": [
                    {"stage": stage, "status": status}
                    for stage, status in requirements[cursor:]
                ],
            }
            if best is None or len(candidate["matched"]) > len(best["matched"]):
                best = candidate
            if candidate["status"] == "RECONSTRUCTABLE":
                break
        report[name] = best or {
            "status": "MISSING_EVIDENCE", "matched": [],
            "missing": [{"stage": stage, "status": status} for stage, status in requirements],
        }
    return {"schema_version": 1, "event_count": len(events), "drills": report}


def reconstruct_from_runtime_zips(paths):
    return reconstruct_drills(iter_audit_events(paths))
