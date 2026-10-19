"""Offline frozen confirmation comparison; optional public Demo price diagnostics."""
from __future__ import annotations
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.sequential_confirmation_research import Context, evaluate

OUT = ROOT / "research_data/sequential_confirmation_20260909"
KEEP = {"RUN_SESSION", "RR_FILTER", "SNIPER", "INVALIDATION_STATE", "ENTRY_ZONE_TOUCH", "ENTRY_CONFIRMATION"}
MINUTE = timedelta(minutes=1)

def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def records(path):
    if path.suffix == ".zip":
        with ZipFile(path) as z:
            if z.testzip() is not None:
                raise ValueError("CRC failed: " + str(path))
            for name in sorted(z.namelist()):
                if "decision_audit" not in name:
                    continue
                with z.open(name) as raw:
                    stream = gzip.GzipFile(fileobj=raw) if name.endswith(".gz") else raw
                    for line in stream:
                        yield json.loads(line)
    else:
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                yield json.loads(line)

def audit():
    protocol = json.loads((OUT / "protocol.json").read_text(encoding="utf-8"))
    assert digest(ROOT / "utils/sequential_confirmation_research.py") == protocol["rule_source_sha256"], "Frozen rule changed"
    kept, seen, sequences, stage_counts = [], set(), defaultdict(set), defaultdict(Counter)
    duplicate_ids = 0
    for source in protocol["sources"]:
        path = ROOT / source["path"]
        assert digest(path) == source["sha256"], path
        for e in records(path):
            key = e.get("event_id")
            if not key:
                raise ValueError("Missing event identity")
            if key in seen:
                duplicate_ids += 1
                continue
            seen.add(key)
            run = e["run_id"]
            sequences[run].add(e["event_sequence"])
            stage_counts[run][e["stage"] + ":" + e["status"]] += 1
            if e["stage"] in KEEP:
                kept.append(e)
    gaps = {}
    for run, seq in sequences.items():
        ordered = sorted(seq)
        gaps[run] = [b for a, b in zip(ordered, ordered[1:]) if b != a + 1]
    kept.sort(key=lambda e: (e["timestamp"], e["event_sequence"]))
    orders, versions, configs, observations = {}, {}, {}, {}
    decisions, extra, reason_counts = [], [], Counter()
    for e in kept:
        run, stage, d = e["run_id"], e["stage"], e.get("details", {})
        if stage == "RUN_SESSION":
            versions[run] = d.get("code_identity", {})
            configs[run] = d.get("configuration_hash", "unknown")
            continue
        key = (run, e.get("trade_id"))
        if not key[1]:
            continue
        if stage == "RR_FILTER" and e["status"] == "PASS":
            plan = d.get("target_plan") or {}
            if plan.get("valid"):
                orders[key] = {"plan": plan, "symbol": e["symbol"], "epoch": 0, "state": "VALID",
                    "touch": None, "deadline": None, "active": False, "created": None, "thesis": None}
        order = orders.get(key)
        if stage == "SNIPER" and e["status"] == "PENDING" and order:
            order.update(active=True, created=e["timestamp"], thesis=d.get("setup_thesis_id"),
                         deadline=utc(d["expires_at"]) if d.get("expires_at") else None)
        elif stage == "SNIPER" and e["status"] != "PENDING" and order:
            order["active"] = False
            order["epoch"] += 1
            observations.pop(key, None)
        elif stage == "INVALIDATION_STATE" and order:
            order["state"] = e["status"]
            order["epoch"] += 1
        elif stage == "ENTRY_ZONE_TOUCH" and order:
            order["touch"] = utc(e["timestamp"])
            window = d.get("confirmation_window_minutes")
            order["deadline"] = order["touch"] + timedelta(minutes=window) if window else None
        elif stage == "ENTRY_CONFIRMATION":
            current = d.get("confirmation") or {}
            alt, prior, context = current, observations.get(key), None
            if order and order["active"] and order["deadline"]:
                side = order["plan"].get("stop_plan", {}).get("side")
                identity = json.dumps([run, key[1], order["thesis"], side, configs.get(run),
                    [order["plan"].get(k) for k in ("entry", "sl", "tp", "min_rr")]], sort_keys=True)
                context = Context(identity, utc(e["timestamp"]), order["touch"], order["deadline"],
                    order["state"], order["epoch"] + bisect_right(gaps[run], e["event_sequence"]))
                alt, observations[key] = evaluate(current, context, prior)
            reason_counts[alt.get("sequential", {}).get("reason", "NO_ACTIVE_RECORDED_PLAN")] += 1
            row = {"run_id": run, "trade_id": key[1], "event_id": e["event_id"],
                "timestamp": e["timestamp"], "symbol": e["symbol"],
                "baseline_pass": current.get("passed") is True, "alternative_pass": alt.get("passed") is True,
                "changed": alt.get("sequential", {}).get("changed", False),
                "failed_gates": current.get("failed_gates"), "diagnostic": alt.get("sequential"),
                "cohort": "development" if run == protocol["development_run"] else "historical_validation"}
            decisions.append(row)
            if row["changed"]:
                extra.append({**row, "plan": order["plan"], "created_at": order["created"],
                    "current_confirmation": current, "previous_confirmation": prior.confirmation,
                    "previous_observed_at": prior.context.observed_at.isoformat(),
                    "zone_touch": order["touch"].isoformat(), "deadline": order["deadline"].isoformat(),
                    "fillability": "UNVERIFIED: no same-time order book or fresh post-confirmation context revalidation"})
    summary = {}
    for cohort in ("development", "historical_validation"):
        rows = [x for x in decisions if x["cohort"] == cohort]
        selected_runs = {x["run_id"] for x in rows}
        if cohort == "development":
            selected_runs.add(protocol["development_run"])
        else:
            selected_runs.update(set(stage_counts) - {protocol["development_run"]})
        summary[cohort] = {
            "runs": len(selected_runs),
            "completed_scans": sum(stage_counts[r]["SCAN_ROUND:COMPLETED"] for r in selected_runs),
            "quality_rejected": sum(stage_counts[r]["QUALITY_FILTER:REJECTED"] for r in selected_runs),
            "rr_rejected": sum(stage_counts[r]["RR_FILTER:REJECTED"] for r in selected_runs),
            "pending_plans": sum(stage_counts[r]["SNIPER:PENDING"] for r in selected_runs),
            "confirmation_observations": len(rows),
            "baseline_confirmed_orders": len({(x["run_id"], x["trade_id"]) for x in rows if x["baseline_pass"]}),
            "alternative_confirmed_orders": len({(x["run_id"], x["trade_id"]) for x in rows if x["alternative_pass"]}),
            "extra_confirmed_orders": len({(x["run_id"], x["trade_id"]) for x in rows if x["changed"]}),
        }
    result = {"protocol_sha256": digest(OUT/"protocol.json"), "unique_events": len(seen),
        "duplicate_event_copies_removed": duplicate_ids, "versions": versions,
        "sequence_gap_boundaries_by_run": gaps, "stage_counts_by_run": dict(stage_counts),
        "summary": summary, "rule_reasons": dict(reason_counts), "observations": decisions, "additional_confirmations": extra,
        "promotion": "NOT_ELIGIBLE; retrospective gate comparison is not executable performance"}
    write_json(OUT / "gate_comparison.json", result)
    return result

def fetch_minutes(case):
    observed = utc(case["timestamp"])
    start = utc(case["previous_confirmation"]["closed_bar"]["open_time"])
    first = observed.replace(second=0, microsecond=0) + MINUTE
    end = first + timedelta(minutes=60)
    symbol = re.sub("[^A-Z0-9]", "", case["symbol"].split(":")[0].upper())
    path = OUT / "public_demo_1m" / f"{symbol}_{int(start.timestamp())}_{int(end.timestamp())}.json"
    if not path.exists():
        url = "https://demo-fapi.binance.com/fapi/v1/klines?" + urlencode({
            "symbol": symbol, "interval": "1m", "startTime": int(start.timestamp()*1000),
            "endTime": int(end.timestamp()*1000)-1, "limit": 1500})
        base = ["curl.exe", "--silent", "--show-error", "--fail-with-body",
            "--connect-timeout", "8", "--max-time", "18"]
        attempts = [base + [url]]
        old = ROOT / "research_data/runtime_review_20260908/demo_1m_manifest.json"
        if old.exists():
            ip = json.loads(old.read_text())["resolved_ip"]
            attempts.append(base + ["--resolve", "demo-fapi.binance.com:443:"+ip, url])
        error = None
        for command in attempts:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=23)
            if proc.returncode == 0:
                rows = json.loads(proc.stdout)
                if isinstance(rows, list) and rows:
                    payload = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "tls_verified": True, "used_cached_dns": "--resolve" in command, "rows": rows}
                    write_json(path, payload)
                    break
            error = proc.stderr[:180]
        else:
            raise RuntimeError(error or "Empty public Demo response")
    payload = json.loads(path.read_text())
    return payload["rows"], {"cache": str(path.relative_to(ROOT)), "sha256": digest(path), "source": "BINANCE_USDM_DEMO_REST", "tls_verified": payload["tls_verified"]}

def verify_recorded(rows, case):
    comparisons = []
    for name in ("previous_confirmation", "current_confirmation"):
        c = case[name]["closed_bar"]
        start = int(utc(c["open_time"]).timestamp()*1000)
        selected = [x for x in rows if start <= x[0] < start + 300_000]
        if [x[0] for x in selected] != list(range(start, start+300_000, 60_000)):
            return {"valid": False, "reason": "MISSING_OR_DUPLICATE_SOURCE_MINUTES"}
        actual = {"open": float(selected[0][1]), "high": max(float(x[2]) for x in selected),
            "low": min(float(x[3]) for x in selected), "close": float(selected[-1][4]),
            "volume": sum(float(x[5]) for x in selected)}
        for k, value in actual.items():
            matched = math.isclose(value, float(c[k]), rel_tol=1e-6, abs_tol=1e-10)
            comparisons.append({"bar": name, "field": k, "recorded": c[k], "downloaded": value, "matched": matched})
    return {"valid": all(x["matched"] for x in comparisons), "comparisons": comparisons}

def simulate(rows, case):
    """A fixed diagnostic; no claim that production would place this order."""
    decision = utc(case["timestamp"])
    first = decision.replace(second=0, microsecond=0) + MINUTE
    end = first + timedelta(minutes=60)
    first_ms, end_ms = int(first.timestamp()*1000), int(end.timestamp()*1000)
    path = [x for x in rows if first_ms <= x[0] < end_ms]
    if not path or path[0][0] != first_ms:
        return {"status": "DATA_GAP_AT_ENTRY"}
    plan = case["plan"]
    side = plan["stop_plan"]["side"]
    if side not in ("buy", "sell"):
        return {"status": "INVALID_SIDE"}
    direction = 1 if side == "buy" else -1
    entry = float(path[0][1]) * (1+direction*3/10000)
    stop, target = float(plan["sl"]), float(plan["tp"])
    risk = direction*(entry-stop)
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop, target)) or risk <= 0 or direction*(target-entry) <= 0:
        return {"status": "INVALID_GEOMETRY_AT_MODELED_ENTRY"}
    rr = direction*(target-entry)/risk
    if rr < float(plan["min_rr"]):
        return {"status": "RR_REJECTED_AT_MODELED_ENTRY", "entry": entry, "rr": rr}
    expected = first_ms
    seen = set()
    def finish(status, bar, raw_price):
        slip = 8 if status == "SL_FIRST" else 2 if status == "TP_FIRST" else 3
        exit_price = raw_price * (1-direction*slip/10000)
        fees = (entry+exit_price)*5/10000
        net = (direction*(exit_price-entry)-fees)/risk
        return {"status": status, "entry_at": first.isoformat(), "exit_bar_open_ms": bar[0],
            "entry_after_friction": entry, "stop": stop, "target": target, "rr_at_entry": rr,
            "exit_after_friction": exit_price, "risk_per_unit": risk, "net_r": net,
            "decision_to_model_entry_seconds": (first-decision).total_seconds()}
    for i, bar in enumerate(path):
        if bar[0] != expected or bar[0] in seen or (i+1<len(path) and path[i+1][0] == bar[0]):
            return {"status": "DATA_GAP_OR_DUPLICATE", "expected_open_ms": expected}
        seen.add(bar[0])
        op, hi, lo, cl = map(float, bar[1:5])
        if not all(math.isfinite(x) and x > 0 for x in (op, hi, lo, cl)) or not lo <= min(op, cl) <= max(op, cl) <= hi:
            return {"status": "INVALID_OHLC"}
        # At the minute open, a stop gap has known temporal precedence over
        # any later high/low in that minute.
        if (side == "buy" and op <= stop) or (side == "sell" and op >= stop):
            return finish("SL_FIRST", bar, op)
        if (side == "buy" and op >= target) or (side == "sell" and op <= target):
            return finish("TP_FIRST", bar, target)
        sl_hit = lo <= stop if side == "buy" else hi >= stop
        tp_hit = hi >= target if side == "buy" else lo <= target
        if sl_hit and tp_hit:
            return {"status": "AMBIGUOUS_BOTH_HIT", "bar_open_ms": bar[0]}
        if sl_hit or tp_hit:
            return finish("SL_FIRST" if sl_hit else "TP_FIRST", bar, stop if sl_hit else target)
        expected += 60_000
    if expected != end_ms:
        return {"status": "RIGHT_CENSORED_OR_DATA_GAP", "expected_open_ms": expected}
    return finish("TIME_EXIT", path[-1], float(path[-1][4]))

def prices(result):
    unique = {}
    for c in result["additional_confirmations"]:
        unique.setdefault((c["run_id"], c["trade_id"]), c)
    outputs = []
    for case in unique.values():
        row = {k: case[k] for k in ("cohort", "symbol", "timestamp", "trade_id", "event_id")}
        try:
            raw, source = fetch_minutes(case)
            verified = verify_recorded(raw, case)
            row.update(source=source, source_verification=verified)
            row["diagnostic"] = simulate(raw, case) if verified["valid"] else {"status": "SOURCE_MISMATCH_QUARANTINED"}
        except Exception as exc:
            row["diagnostic"] = {"status": "DATA_UNAVAILABLE", "reason": str(exc)}
        outputs.append(row)
        print(json.dumps({"symbol": row["symbol"], "cohort": row["cohort"], "diagnostic": row["diagnostic"]}, ensure_ascii=True), flush=True)
    payload = {"protocol_sha256": result["protocol_sha256"], "cases": outputs,
        "limitations": ["Diagnostic fixed exits; no order book, fresh-context revalidation, production trailing, funding, portfolio sizing or actual executions.",
            "Historical validation data was previously available; not prospective untouched data."],
        "promotion": "NOT_ELIGIBLE"}
    write_json(OUT/"price_diagnostics.json", payload)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prices", action="store_true", help="Read public Demo klines; never authenticate or place orders")
    args = parser.parse_args()
    if args.prices:
        payload = json.loads((OUT/"gate_comparison.json").read_text(encoding="utf-8"))
        assert digest(OUT/"protocol.json") == payload["protocol_sha256"]
        prices(payload)
    else:
        payload = audit()
        print(json.dumps({"events": payload["unique_events"], "summary": payload["summary"],
            "reasons": payload["rule_reasons"],
            "additional": [{k:x[k] for k in ("cohort","symbol","timestamp")} for x in payload["additional_confirmations"]]}, ensure_ascii=True, indent=2))
