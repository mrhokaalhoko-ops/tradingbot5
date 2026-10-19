"""Offline multi-symbol timing audit of the three supplied September 8 archives."""
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "402ad3b76ba04c41946588ed81ff92f6"


def ts(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Audit timestamp lacks a timezone")
    return result.timestamp()


def stats(values):
    values = sorted(values)
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": round(values[0], 3),
            "median": round(median(values), 3),
            "p95_nearest_rank": round(values[math.ceil(.95 * len(values)) - 1], 3),
            "max": round(values[-1], 3)}


def build():
    events, archives = [], []
    for part in ("01", "02", "03"):
        p = ROOT / f"runtime_logs_24h_20260908_1939_part{part}.zip"
        with ZipFile(p) as z:
            assert z.testzip() is None
            events.extend(json.loads(line) for line in z.open("decision_audit_24h.jsonl"))
        archives.append({"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    assert len({e["event_id"] for e in events}) == len(events)
    rows = sorted((e for e in events if e["run_id"] == RUN_ID), key=lambda e: e["event_sequence"])
    assert [e["event_sequence"] for e in rows] == list(range(1, len(rows) + 1))
    select = lambda stage, status=None: [e for e in rows if e["stage"] == stage and (status is None or e["status"] == status)]
    rounds = {e["scan_id"]: e for e in select("SCAN_ROUND", "STARTED")}
    universe = {e["scan_id"]: e for e in select("SCANNER_UNIVERSE")}
    assert set(rounds) == set(universe) == {e["scan_id"] for e in select("SCAN_ROUND", "COMPLETED")}
    starts = sorted(ts(e["timestamp"]) for e in rounds.values())
    cohorts = {}
    for name, added in (("activity_added", True), ("liquidity_retained", False)):
        chosen = [e for e in select("HYBRID_DECISION") if
                  (e["symbol"] in {s.split(":")[0] for s in universe[e["scan_id"]]["details"]["added_vs_liquidity"]}) == added]
        cohorts[name] = {"unique_symbols": len({e["symbol"] for e in chosen}),
            "decisions": len(chosen),
            "seconds_since_round_start": stats([ts(e["timestamp"]) - ts(rounds[e["scan_id"]]["timestamp"]) for e in chosen]),
            "seconds_since_scheduled_15m_boundary": stats([ts(e["timestamp"]) - math.floor(ts(rounds[e["scan_id"]]["timestamp"]) / 900) * 900 for e in chosen])}
    assert sum(c["decisions"] for c in cohorts.values()) + len(select("SCAN_SKIP", "PENDING_ORDER")) == len(select("SCAN_STARTED"))
    return {"run_id": RUN_ID, "archives": archives, "events_all_runs": len(events),
        "run_first_utc": rows[0]["timestamp"], "run_last_utc": rows[-1]["timestamp"],
        "completed_rounds": len(rounds), "unique_scanned_symbols": len({e["symbol"] for e in select("SCAN_STARTED")}),
        "directional_signal_symbols": len({e["symbol"] for e in select("HYBRID_DECISION") if e["status"] in ("BUY", "SELL")}),
        "round_interval_seconds": stats([b-a for a, b in zip(starts, starts[1:])]),
        "round_duration_seconds": stats([e["details"]["duration_seconds"] for e in select("SCAN_ROUND", "COMPLETED")]),
        "preflight_seconds": stats([ts(e["timestamp"]) - ts(rounds[k]["timestamp"]) for k, e in universe.items()]),
        "cohorts": cohorts,
        "rr_rejections": dict(Counter(e["details"]["target_plan"].get("reason") for e in select("RR_FILTER", "REJECTED"))),
        "quality_rejections_by_symbol": dict(Counter(e["symbol"] for e in select("QUALITY_FILTER", "REJECTED"))),
        "limitations": ["Source run is 1.6.9, not live 1.6.10 data.",
            "Preflight includes reconciliation, risk, fill queries and universe selection; its full duration cannot be attributed to one call.",
            "Timing is measured from scheduling/decision events, not from the unknown onset of a tradable opportunity.",
            "This is not a 5m strategy replay or profitability estimate. Cohorts may contain the same symbol in different rounds."]}


if __name__ == "__main__":
    result = build()
    output = ROOT / "audit_exports/scanner_latency_20260909.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("completed_rounds", "unique_scanned_symbols", "directional_signal_symbols", "round_duration_seconds", "preflight_seconds")}, ensure_ascii=True))
