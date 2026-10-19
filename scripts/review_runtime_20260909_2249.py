"""Read the three supplied ZIPs offline; write only local review artifacts."""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
STEM = "runtime_review_20260909_2249"
ADEN = timezone(timedelta(hours=3))

def local(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ADEN).strftime("%Y-%m-%d %H:%M:%S")

def counts(rows):
    return dict(sorted(Counter(e["stage"] + ":" + e["status"] for e in rows).items()))

def select(rows, stage, status=None):
    return [e for e in rows if e["stage"] == stage and (status is None or e["status"] == status)]

def canon(symbol):
    return (symbol or "").split(":")[0].replace("/", "")

def build():
    events, archives, seen, activity = [], [], set(), []
    duplicates = malformed = 0
    for part in ("01", "02", "03"):
        path = ROOT / f"runtime_logs_24h_20260909_2249_part{part}.zip"
        with ZipFile(path) as z:
            corrupt = z.testzip()
            if corrupt:
                raise ValueError(f"Corrupt member: {path.name}/{corrupt}")
            archives.append({"name": path.name, "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "crc_ok": True,
                "members": {i.filename: i.file_size for i in z.infolist()},
                "exporter_summary": z.read("summary.txt").decode("utf-8")})
            activity.extend(z.read("bot_activity_24h.log").decode("utf-8").splitlines())
            for line in z.open("decision_audit_24h.jsonl"):
                try:
                    e = json.loads(line)
                    event_id = e["event_id"]
                except (ValueError, KeyError):
                    malformed += 1
                    continue
                if event_id in seen:
                    duplicates += 1
                    continue
                seen.add(event_id)
                events.append(e)
    events.sort(key=lambda e: (e["timestamp"], e["event_sequence"]))
    print("Parsed and checked archives:", len(events), flush=True)
    latest = max(select(events, "RUN_SESSION"), key=lambda e: e["timestamp"])
    current = [e for e in events if e["run_id"] == latest["run_id"]]
    byrun = defaultdict(list)
    for e in events:
        byrun[e["run_id"]].append(e)
    runs = []
    for run_id, rows in byrun.items():
        session = next(iter(select(rows, "RUN_SESSION")), None)
        seq = [e["event_sequence"] for e in rows]
        runs.append({"run_id": run_id, "first": rows[0]["timestamp"], "last": rows[-1]["timestamp"],
            "events": len(rows), "sequence_min": min(seq), "sequence_max": max(seq),
            "internal_sequence_gaps": max(seq) - min(seq) + 1 - len(set(seq)),
            "duplicate_sequences": len(seq) - len(set(seq)),
            "code_identity": session["details"].get("code_identity") if session else None,
            "configuration": session["details"].get("configuration") if session else None,
            "stage_counts": counts(rows)})
    uni = {e["scan_id"]: e["details"] for e in select(current, "SCANNER_UNIVERSE")}
    activity_symbols = {scan: {canon(s) for s in d["added_vs_liquidity"]} for scan, d in uni.items()}
    def cohort(e):
        d = uni.get(e["scan_id"])
        if d is None:
            return "unmapped"
        return "activity_added" if canon(e["symbol"]) in activity_symbols[e["scan_id"]] else "liquidity_retained"
    mapping = {e["decision_id"]: e.get("thesis_id") or e.get("setup_id") for e in select(current, "SETUP_CREATED")}
    def identity(e):
        return e.get("thesis_id") or e.get("setup_id") or e["details"].get("setup_thesis_id") or mapping.get(e["decision_id"])
    stages = [("SETUP_CREATED", "ACTIVE"), ("QUALITY_FILTER", "PASS"), ("RR_FILTER", "PASS"), ("ENTRY_ZONE_TOUCH", "TOUCHED"), ("ENTRY_CONFIRMATION", "PASS")]
    sets = [{identity(e) for e in select(current, s, st)} - {None} for s, st in stages]
    disjoint = [len(sets[i] - sets[i + 1]) for i in range(len(sets) - 1)]
    assert all(b <= a for a, b in zip(sets, sets[1:]))
    assert sum(disjoint) + len(sets[-1]) == len(sets[0])
    pending = []
    for e in select(current, "SNIPER", "PENDING"):
        related = [x for x in current if x.get("trade_id") == e["trade_id"]]
        confirmations = select(related, "ENTRY_CONFIRMATION")
        terminal = [x for x in select(related, "SNIPER") if x["status"] != "PENDING"]
        plan = confirmations[0]["details"]["target_plan"]
        pending.append({"trade_id": e["trade_id"], "thesis_id": identity(e), "symbol": e["symbol"],
            "cohort": cohort(e), "created_utc": e["timestamp"], "created_aden": local(e["timestamp"]),
            "side": plan["stop_plan"]["side"], "entry": plan["entry"], "sl": plan["sl"], "tp": plan["tp"], "rr": plan["rr"],
            "touches": [{"timestamp": x["timestamp"], **x["details"]} for x in select(related, "ENTRY_ZONE_TOUCH")],
            "confirmations": [{"timestamp": x["timestamp"], "event_id": x["event_id"], **x["details"]["confirmation"]} for x in confirmations],
            "terminal": [{"timestamp": x["timestamp"], "status": x["status"], "details": x["details"]} for x in terminal],
            "evidence_event_ids": [x["event_id"] for x in related if x["stage"] in {"SNIPER", "ENTRY_ZONE_TOUCH", "ENTRY_CONFIRMATION", "ENTRY_PRICE_BASIS", "INVALIDATION_STATE"}]})
    print("Reconciled runs, theses and pending plans", flush=True)
    shadow = {}
    for e in select(current, "ACTIVE_ALTCOIN_SHADOW"):
        if not e["status"].startswith("OUTCOME_"):
            continue
        p, o = e["details"]["plan"], e["details"]["outcome"]
        shadow[p["plan_id"]] = {"symbol": e["symbol"], "event_id": e["event_id"],
            "execution_enabled": e["details"]["execution_enabled"], "reported_at": e["timestamp"],
            "plan": {k: p.get(k) for k in ("plan_id", "setup_id", "side", "filled_at", "observed_at", "observation_delay_seconds", "entry", "stop", "target", "round_trip_cost_bps")},
            "recorded_outcome": o}
    resources = select(current, "RESOURCE_SNAPSHOT")
    warm = [e["details"]["resource"] for e in resources if e["details"]["resource"]["uptime_seconds"] >= 3600]
    resource_summary = {k: {"first_after_one_hour": warm[0][k], "last": warm[-1][k], "min_after_one_hour": min(x[k] for x in warm), "max_after_one_hour": max(x[k] for x in warm)}
        for k in ("rss_mb", "threads", "file_descriptors", "cgroup_current_mb", "cgroup_oom_events", "cgroup_oom_kill_events")}
    levels = Counter()
    errors = []
    monitor_warnings = 0
    for line in activity:
        match = re.search(r" - (WARNING|ERROR|CRITICAL) - (.*)", line)
        if not match:
            continue
        level, message = match.groups()
        levels[level] += 1
        if level in {"ERROR", "CRITICAL"}:
            errors.append({"timestamp": line[:24], "message": message})
        if level == "WARNING" and "[Network Monitor]" in message:
            monitor_warnings += 1
    net = [x["recorded_outcome"]["net_r"] for x in shadow.values()]
    total = counts(current)
    assert malformed == 0 and duplicates == 0
    assert all(x["internal_sequence_gaps"] == x["duplicate_sequences"] == 0 for x in runs)
    assert total["SCAN_STARTED:STARTED"] == total["HYBRID_DECISION:HOLD"] + total["HYBRID_DECISION:BUY"] + total["HYBRID_DECISION:SELL"] + total.get("SCAN_SKIP:PENDING_ORDER", 0)
    assert all(d["selected_count"] == len(d["targets"]) == 50 for d in uni.values())
    assert all(len(p["terminal"]) == 1 for p in pending)
    assert all(not x["execution_enabled"] for x in shadow.values())
    print("Reconciled shadow results and resources", flush=True)
    result = {
        "method": "Offline raw audit events, deduplicated by event_id; current run isolated; no market download or counterfactual replay.",
        "archives": archives, "unique_events": len(events), "malformed": malformed, "duplicates": duplicates,
        "first_event_utc": events[0]["timestamp"], "last_event_utc": events[-1]["timestamp"],
        "first_event_aden": local(events[0]["timestamp"]), "last_event_aden": local(events[-1]["timestamp"]),
        "runs": runs, "all_stage_counts": counts(events),
        "current_run_id": latest["run_id"], "code_identity": latest["details"]["code_identity"],
        "configuration": latest["details"]["configuration"],
        "entry_margin_percent_logged": "entry_margin_percent" in latest["details"]["configuration"],
        "current_stage_counts": total,
        "theses_reaching_stages": {s + ":" + st: len(v) for (s, st), v in zip(stages, sets)},
        "disjoint_deepest_stage_counts": dict(zip(("never_quality_passed", "quality_passed_never_rr_passed", "rr_passed_never_touched", "touched_without_confirmation_pass"), disjoint)),
        "quality_rejection_gates": dict(Counter(e["details"]["gates"]["terminal_gate"] for e in select(current, "QUALITY_FILTER", "REJECTED"))),
        "confirmation_failed_gates_overlapping": dict(Counter(g for e in select(current, "ENTRY_CONFIRMATION") for g in e["details"]["confirmation"]["failed_gates"])),
        "universe": {"scans": len(uni), "activity_status": dict(Counter(d["activity_status"] for d in uni.values())),
            "new_unique_symbols": len({s for d in uni.values() for s in d["added_vs_liquidity"]}),
            "new_symbol_attempts": sum(len(d["added_vs_liquidity"]) for d in uni.values()),
            "additions_min": min(len(d["added_vs_liquidity"]) for d in uni.values()),
            "additions_max": max(len(d["added_vs_liquidity"]) for d in uni.values()),
            "cohort_counts": {c: counts([e for e in current if e["symbol"] and cohort(e) == c]) for c in ("activity_added", "liquidity_retained", "unmapped")}},
        "pending_plans": pending,
        "shadow_outcomes": list(shadow.values()), "shadow_net_r_sum": sum(net), "shadow_net_r_mean": sum(net)/len(net),
        "resource_summary_after_one_hour": resource_summary,
        "last_resource_snapshot": resources[-1],
        "activity_log_levels_all_runs": dict(levels), "network_monitor_warnings_all_runs": monitor_warnings,
        "activity_errors_all_runs": errors,
        "limitations": [
            "Requested 24h window: 2026-09-08 22:49:09 UTC through 2026-09-09 22:49:09 UTC; raw events begin 25 seconds after window start.",
            "No exchange account statement in ZIPs; zero execution events observed, not an independent exchange reconciliation.",
            "Activity cohort describes additions beyond each scan liquidity baseline, not a controlled experiment on displaced symbols.",
            "Shadow entries/outcomes are model outputs, not executable fills or account profit; observations arrive about 40 seconds after assumed entry.",
            "Local unreleased changes are not included in deployed commit 44a7ce1.",
        ],
        "integrity_checks": "CRC, JSON parsing, event uniqueness, per-run sequence continuity, scan conservation, thesis set nesting, universe size and terminal plan reconciliation passed.",
    }
    result["momentum_outcomes"] = [
        {"timestamp": e["timestamp"], "symbol": e["symbol"], "event_id": e["event_id"],
         "status": e["status"], "details": e["details"]}
        for e in select(current, "MOMENTUM_CONTINUATION") if e["status"].startswith("OUTCOME_")
    ]
    result["rr_rejection_reasons"] = dict(Counter(
        e["details"]["target_plan"].get("reason") for e in select(current, "RR_FILTER", "REJECTED")))
    result["price_basis_events"] = [e for e in select(current, "ENTRY_PRICE_BASIS")]
    result["price_basis_guard_reasons"] = dict(Counter(
        e["details"].get("executable_guard", {}).get("reason")
        for e in select(current, "ENTRY_PRICE_BASIS", "EXECUTABLE_GUARD_REJECTED")))
    result["all_directional_theses"] = len({identity(e) for e in current
        if e["stage"] == "HYBRID_DECISION" and e["status"] in {"BUY", "SELL"}} - {None})
    pending_ids = {p["decision_id"] for p in select(current, "SNIPER", "PENDING")}
    result["market_context_for_pending"] = [e for e in select(current, "MARKET_CONTEXT")
        if e["decision_id"] in pending_ids]
    durations = [e["details"]["duration_seconds"] for e in select(current, "SCAN_ROUND", "COMPLETED")]
    result["scan_durations_seconds"] = {"min": min(durations), "max": max(durations),
        "mean": sum(durations) / len(durations), "median": sorted(durations)[len(durations)//2]}
    directional = total.get("HYBRID_DECISION:BUY", 0) + total.get("HYBRID_DECISION:SELL", 0)
    assert directional == total["SETUP_CREATED:ACTIVE"] + total.get("SETUP_COOLDOWN:REJECTED", 0)
    assert total["SETUP_CREATED:ACTIVE"] == total["QUALITY_FILTER:REJECTED"] + total["QUALITY_FILTER:PASS"] + total.get("DERIVATIVES_FILTER:BLOCK_SUGGESTED", 0)
    assert total["QUALITY_FILTER:PASS"] == total["RR_FILTER:REJECTED"] + total["RR_FILTER:PASS"]
    assert total["RR_FILTER:PASS"] == len(pending)
    return result

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    r = build()
    path = ROOT / "audit_exports" / (STEM + ".json")
    path.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: r[k] for k in (
        "unique_events", "current_stage_counts", "theses_reaching_stages",
        "all_directional_theses", "disjoint_deepest_stage_counts",
        "quality_rejection_gates", "rr_rejection_reasons", "price_basis_guard_reasons",
        "resource_summary_after_one_hour", "scan_durations_seconds", "shadow_net_r_sum")},
        ensure_ascii=False, indent=2))
    print(path)
