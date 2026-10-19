"""Reproduce the local review's runtime counts without credentials or network."""
from __future__ import annotations
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "audit_exports" / "independent_review_20260907.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    events, seen, invalid, duplicates = [], set(), 0, 0
    archives = sorted(ROOT.glob("runtime_logs_24h_20260907_1905_part*.zip"))
    if len(archives) != 4:
        raise ValueError("Expected all four runtime archive parts")
    for path in archives:
        with ZipFile(path) as archive:
            for line in archive.read("decision_audit_24h.jsonl").splitlines():
                try:
                    event = json.loads(line)
                    event_id = event["event_id"]
                except (ValueError, KeyError):
                    invalid += 1
                    continue
                if event_id in seen:
                    duplicates += 1
                    continue
                seen.add(event_id)
                events.append(event)
    sessions = [e for e in events if e["stage"] == "RUN_SESSION"]
    latest = max(sessions, key=lambda e: e["timestamp"])
    current = [e for e in events if e["run_id"] == latest["run_id"]]

    def funnel(rows):
        mapping = {
            e["decision_id"]: e.get("thesis_id") or e.get("setup_id")
            for e in rows if e["stage"] == "SETUP_CREATED"
        }
        def identity(e):
            return (e.get("thesis_id") or e.get("setup_id")
                    or e["details"].get("setup_thesis_id") or mapping.get(e.get("decision_id")))
        thesis_ids = set(mapping.values()) - {None}
        stages = [
            ("QUALITY_FILTER", "PASS"), ("QUALITY_FILTER", "REJECTED"),
            ("RR_FILTER", "PASS"), ("RR_FILTER", "REJECTED"),
            ("SNIPER", "PENDING"), ("ENTRY_ZONE_TOUCH", "TOUCHED"),
            ("ENTRY_CONFIRMATION", "PASS"), ("EXECUTION_TIMELINE", "GATE_ENTERED"),
        ]
        counts, sets = {}, {}
        for stage, status in stages:
            selected = [e for e in rows if (e["stage"], e["status"]) == (stage, status)]
            key = stage + ":" + status
            sets[key] = {identity(e) for e in selected} - {None}
            counts[key] = {"events": len(selected), "theses": len(sets[key]),
                           "unmapped": sum(identity(e) is None for e in selected)}
        quality = sets["QUALITY_FILTER:PASS"]
        rr = sets["RR_FILTER:PASS"]
        touch = sets["ENTRY_ZONE_TOUCH:TOUCHED"]
        return {
            "events": len(rows), "independent_theses": len(thesis_ids),
            "completed_scans": sum(e["stage"] == "SCAN_ROUND" and e["status"] == "COMPLETED" for e in rows),
            "hybrid_buy_sell": sum(e["stage"] == "HYBRID_DECISION" and e["status"] in {"BUY", "SELL"} for e in rows),
            "stage_counts": counts,
            "disjoint_deepest_stage": {
                "never_quality_passed": len(thesis_ids - quality),
                "quality_passed_never_rr_passed": len(quality - rr),
                "rr_passed_never_touched": len(rr - touch),
                "touched_without_confirmation_pass": len(touch - sets["ENTRY_CONFIRMATION:PASS"]),
            },
            "sniper_orders": [{
                "symbol": e["symbol"], "timestamp": e["timestamp"], "thesis_id": identity(e),
                "entry": e["details"]["optimal_entry"], "expires_at": e["details"]["expires_at"],
            } for e in rows if (e["stage"], e["status"]) == ("SNIPER", "PENDING")],
        }

    outcomes = {}
    for e in events:
        if e["stage"] not in {"ACTIVE_ALTCOIN_SHADOW", "MOMENTUM_CONTINUATION"} or not e["status"].startswith("OUTCOME_"):
            continue
        plan, outcome = e["details"]["plan"], e["details"]["outcome"]
        outcomes[(e["stage"], plan["plan_id"])] = {
            "stage": e["stage"], "symbol": e["symbol"], "reported_at": e["timestamp"],
            "plan": {k: plan.get(k) for k in (
                "plan_id", "side", "entry", "stop", "sl", "target", "tp", "filled_at", "created_at"
            )}, "outcome_as_recorded_not_replayed": outcome,
        }
    resources = [e["details"]["resource"] for e in events if e["stage"] == "RESOURCE_SNAPSHOT"]
    resource_summary = {}
    for key in ("rss_mb", "cgroup_current_mb", "threads", "file_descriptors", "cgroup_oom_events", "cgroup_oom_kill_events"):
        values = [r[key] for r in resources if isinstance(r.get(key), (int, float))]
        resource_summary[key + "_max"] = max(values) if values else None
    headroom = [r["cgroup_headroom_mb"] for r in resources if isinstance(r.get("cgroup_headroom_mb"), (int, float))]
    resource_summary["cgroup_headroom_mb_min"] = min(headroom) if headroom else None
    geometry = []
    historical = json.loads((ROOT / "research_data/active_altcoin_1m/replay_result.json").read_text(encoding="utf-8"))
    filled = [r for r in historical["ledger"] if r.get("filled_at")]
    for row in filled:
        risk = row["entry"] - row["stop"] if row["side"] == "LONG" else row["stop"] - row["entry"]
        if risk <= 0:
            geometry.append({k: row[k] for k in ("symbol", "side", "entry", "stop", "filled_at")})
    code_paths = [
        "main.py", "core/exchange.py", "core/version.py", "engines/ta_engine.py",
        "strategy/active_altcoin_shadow.py", "utils/active_altcoin_shadow_monitor.py",
        "trading_v2/active_altcoin_scalper.py", "scripts/replay_active_altcoin_1m.py",
        "tests/test_shadow_lifecycle_regressions.py",
    ]
    return {
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "code_sha256": {p: digest(ROOT / p) for p in code_paths},
        "archives_sha256": {p.name: digest(p) for p in archives},
        "invalid_json": invalid, "duplicate_event_ids": duplicates,
        "first_event": min(e["timestamp"] for e in events),
        "last_event": max(e["timestamp"] for e in events),
        "recorded_runtime_identity": latest["details"]["code_identity"],
        "all_runs": funnel(events), "latest_run": funnel(current),
        "recorded_shadow_outcomes": list(outcomes.values()),
        "venue_unavailable_all": sum(e["status"] == "SYMBOL_UNAVAILABLE_ON_EXECUTION_VENUE" for e in events),
        "venue_unavailable_latest_run": sum(e["status"] == "SYMBOL_UNAVAILABLE_ON_EXECUTION_VENUE" for e in current),
        "resources": resource_summary,
        "saved_historical_fills": len(filled), "invalid_stop_geometry": geometry,
        "latest_1m_replay_status": "BLOCKED_DEMO_DNS_NO_SOURCE_VERIFICATION_OR_NEW_REPLAY",
        "scope": "Local source and archive review. No orders, deployment, model retraining or threshold tuning.",
    }


if __name__ == "__main__":
    result = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(OUTPUT), "events": result["all_runs"]["events"],
        "latest_run_funnel": result["latest_run"]["disjoint_deepest_stage"],
        "resources": result["resources"],
    }, ensure_ascii=True, indent=2))
