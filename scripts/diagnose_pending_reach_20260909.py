"""Read-only public Demo price-path diagnostics; never an order/fill simulator."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research_data/pending_reach_20260909"
REVIEW = ROOT / "audit_exports/runtime_review_20260909_2249.json"
STEP = 60000
PROTOCOL = {
    "cohort": "All 11 pending orders in the reviewed 1.6.10 run, no outcome selection",
    "primary_window": "Exact recorded order creation through terminal audit timestamp",
    "secondary_window": "Original 30 minute discovery TTL, ignoring actual earlier cancellation; descriptive only",
    "rule": "Use only complete 1m bars contained in the window; boundary bars reported separately",
    "coverage": "Every expected open time required once; missing/duplicate/invalid OHLCV rejects window",
    "touch": "Contract trade OHLC intersects original entry tolerance; not an executable quote or fill",
    "source_check": "Compare downloaded 1m aggregates against every recorded 5m confirmation candle",
    "no_policy_change": "No tuning, TTL extension, gate bypass, orders or profitability inference",
}

def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

def ms(value, *, ceiling=False):
    delta = utc(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    return (micros + 999) // 1000 if ceiling else micros // 1000

def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def intersects(bar, lower, upper):
    return float(bar[3]) <= upper and float(bar[2]) >= lower

def measure(rows, *, start, end, entry, tolerance):
    if not (math.isfinite(entry) and entry > 0 and math.isfinite(tolerance) and tolerance >= 0 and end > start):
        raise ValueError("Invalid window/entry")
    first = (start + STEP - 1) // STEP * STEP
    last = end // STEP * STEP
    expected = list(range(first, last, STEP))
    whole = [r for r in rows if r[0] >= start and r[0] + STEP <= end]
    if [r[0] for r in whole] != expected:
        return {"status": "INCOMPLETE_OR_DUPLICATE_DATA"}
    for r in whole:
        o, h, l, c, v = map(float, r[1:6])
        if not all(math.isfinite(x) for x in (o, h, l, c, v)) or not 0 < l <= min(o, c) <= max(o, c) <= h or v < 0:
            return {"status": "INVALID_OHLCV"}
    lower, upper = entry - tolerance, entry + tolerance
    hits = [r for r in whole if intersects(r, lower, upper)]
    boundary = [r for r in rows if r[0] < end and r[0] + STEP > start and r not in whole]
    gaps = [max(float(r[3]) - upper, lower - float(r[2]), 0) for r in whole]
    return {"status": "OHLC_INTERSECTION" if hits else "NO_COMPLETE_MINUTES" if not whole else "NO_WHOLE_MINUTE_INTERSECTION",
            "complete_minutes": len(whole), "intersection_minutes": len(hits),
            "first_intersection_open": iso(hits[0][0]) if hits else None,
            "closest_distance_bps": min(gaps) / entry * 10000 if gaps else None,
            "boundary_intersections_uncertain": [iso(r[0]) for r in boundary if intersects(r, lower, upper)]}

def fetch(case, start, end):
    path = OUT / "public_demo" / (case["trade_id"] + ".json")
    symbol = case["symbol"].replace("/", "")
    url = "https://demo-fapi.binance.com/fapi/v1/klines?" + urlencode({
        "symbol": symbol, "interval": "1m", "startTime": start, "endTime": end - 1, "limit": 1000})
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        assert cached["url"] == url
        return cached["rows"], path
    ip = json.loads((ROOT / "research_data/runtime_review_20260908/demo_1m_manifest.json").read_text())["resolved_ip"]
    cmd = ["curl.exe", "--silent", "--show-error", "--fail-with-body", "--connect-timeout", "8", "--max-time", "20",
           "--resolve", "demo-fapi.binance.com:443:" + ip, url]
    response = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    if response.returncode:
        raise RuntimeError(response.stderr)
    rows = json.loads(response.stdout)
    if not isinstance(rows, list) or [r[0] for r in rows] != list(range(start, end, STEP)):
        raise ValueError("Downloaded data missing or out of sequence: " + symbol)
    save(path, {"url": url, "tls_verified": True, "cached_dns": True,
                "fetched_at": datetime.now(timezone.utc).isoformat(), "rows": rows})
    print("Downloaded", symbol, len(rows), "minutes", flush=True)
    return rows, path

def verify_candles(case, rows):
    checks = []
    for obs in case["confirmations"]:
        bar = obs["closed_bar"]
        start = ms(bar["open_time"])
        part = [r for r in rows if start <= r[0] < start + 5 * STEP]
        if [r[0] for r in part] != list(range(start, start + 5 * STEP, STEP)):
            checks.append({"event_id": obs["event_id"], "matched": False, "reason": "MISSING_SOURCE"})
            continue
        values = {"open": float(part[0][1]), "high": max(float(r[2]) for r in part),
                  "low": min(float(r[3]) for r in part), "close": float(part[-1][4]),
                  "volume": sum(float(r[5]) for r in part)}
        mismatch = [k for k, v in values.items() if not math.isclose(v, float(bar[k]), rel_tol=1e-9, abs_tol=1e-12)]
        checks.append({"event_id": obs["event_id"], "matched": not mismatch, "mismatched_fields": mismatch})
    return checks

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    protocol_file = OUT / "protocol.json"
    if protocol_file.exists():
        assert json.loads(protocol_file.read_text(encoding="utf-8")) == PROTOCOL
    else:
        save(protocol_file, PROTOCOL)
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    results = []
    for case in review["pending_plans"]:
        created = case["terminal"][0]["details"]["created_at"]
        start = ms(created, ceiling=True)
        end = ms(case["terminal"][0]["timestamp"])
        ttl = ms(created) + 30 * STEP
        rows, path = fetch(case, start // STEP * STEP - 20 * STEP, ((max(end, ttl) + STEP - 1) // STEP + 1) * STEP)
        checks = verify_candles(case, rows)
        obs = case["confirmations"][0]
        tolerance = obs["evidence"]["zone_tolerance_pct"] * case["entry"]
        basis = [e for e in review["price_basis_events"] if e.get("trade_id") == case["trade_id"]]
        good = all(c["matched"] for c in checks)
        item = {"trade_id": case["trade_id"], "thesis_id": case["thesis_id"], "symbol": case["symbol"],
                "side": case["side"], "created_at": iso(start), "ended_at": iso(end), "entry": case["entry"],
                "tolerance": tolerance, "reference_distance_bps": abs(obs["inputs"]["reference_price"] - case["entry"]) / case["entry"] * 10000,
                "terminal": case["terminal"][0]["status"], "recorded_touches": len(case["touches"]),
                "basis_reasons": dict(Counter(e["details"].get("executable_guard", {}).get("reason", e["status"]) for e in basis)),
                "source_checks": checks, "source_matched": good,
                "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source_file": str(path.relative_to(ROOT)),
                "active": measure(rows, start=start, end=end, entry=case["entry"], tolerance=tolerance) if good else {"status": "SOURCE_MISMATCH"},
                "original_30m_descriptive": measure(rows, start=start, end=ttl, entry=case["entry"], tolerance=tolerance) if good else {"status": "SOURCE_MISMATCH"}}
        results.append(item)
        print(case["symbol"], item["active"]["status"], "source_matched=", good, flush=True)
    payload = {"protocol_sha256": hashlib.sha256(protocol_file.read_bytes()).hexdigest(),
               "review_sha256": hashlib.sha256(REVIEW.read_bytes()).hexdigest(),
               "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "results": results, "active_status_counts": dict(Counter(x["active"]["status"] for x in results))}
    save(OUT / "result.json", payload)
    print(json.dumps(payload["active_status_counts"], indent=2))

if __name__ == "__main__":
    main()
