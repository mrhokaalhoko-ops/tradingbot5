"""Measure entry-zone reach, miss distance, and TTL sensitivity on Demo 1m."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_runtime_theses_1m import load_cached_1m, parse_utc
from scripts.replay_runtime_theses_1m import snapshot_plan_inputs
from strategy.entry_confirmation import entry_zone_tolerance
from trading_v2.models import Bar


TTL_MINUTES = (20, 45, 90, 180)


def _iso(value: datetime | None) -> str | None:
    return (
        value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if value is not None else None
    )


def distance_to_zone(bar: Bar, *, entry: float, tolerance: float) -> float:
    lower = float(entry) - float(tolerance)
    upper = float(entry) + float(tolerance)
    if float(bar.low) > upper:
        return float(bar.low) - upper
    if float(bar.high) < lower:
        return lower - float(bar.high)
    return 0.0


def reach_metrics(
    bars: tuple[Bar, ...], *, at: datetime, entry: float, atr: float
) -> dict:
    at = parse_utc(at)
    tolerance = entry_zone_tolerance(entry, atr)
    result = {
        "entry": entry,
        "atr": atr,
        "tolerance": tolerance,
        "tolerance_bps": tolerance / entry * 10_000.0,
        "ttl": {},
    }
    for ttl in TTL_MINUTES:
        end = at + timedelta(minutes=ttl)
        visible = [bar for bar in bars if bar.open_time >= at and bar.open_time < end]
        if not visible or visible[-1].close_time < end:
            result["ttl"][str(ttl)] = {"status": "INSUFFICIENT_DATA"}
            continue
        distances = [distance_to_zone(bar, entry=entry, tolerance=tolerance) for bar in visible]
        touched_index = next((index for index, value in enumerate(distances) if value <= 0), None)
        closest = min(distances)
        item = {
            "status": "TOUCHED" if touched_index is not None else "MISSED",
            "closest_distance": closest,
            "closest_distance_bps": closest / entry * 10_000.0,
            "closest_distance_atr": closest / atr if atr > 0 else None,
        }
        if touched_index is not None:
            touch = visible[touched_index]
            item.update({
                "touch_at": _iso(touch.close_time),
                "minutes_to_touch": (touch.close_time - at).total_seconds() / 60.0,
            })
        result["ttl"][str(ttl)] = item
    return result


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for cohort in sorted({row["cohort"] for row in rows}):
        selected = [row for row in rows if row["cohort"] == cohort]
        cohort_summary = {"count": len(selected), "ttl": {}}
        for ttl in TTL_MINUTES:
            items = [row["metrics"]["ttl"][str(ttl)] for row in selected]
            available = [item for item in items if item["status"] != "INSUFFICIENT_DATA"]
            touched = [item for item in available if item["status"] == "TOUCHED"]
            missed = [item for item in available if item["status"] == "MISSED"]
            miss_bps = [item["closest_distance_bps"] for item in missed]
            touch_times = [item["minutes_to_touch"] for item in touched]
            cohort_summary["ttl"][str(ttl)] = {
                "available": len(available),
                "touched": len(touched),
                "touch_rate": len(touched) / len(available) if available else None,
                "missed": len(missed),
                "miss_distance_bps_median": statistics.median(miss_bps) if miss_bps else None,
                "miss_distance_bps_p75": _percentile(miss_bps, 0.75),
                "miss_distance_bps_p90": _percentile(miss_bps, 0.90),
                "minutes_to_touch_median": statistics.median(touch_times) if touch_times else None,
                "minutes_to_touch_p75": _percentile(touch_times, 0.75),
                "minutes_to_touch_p90": _percentile(touch_times, 0.90),
            }
        summary[cohort] = cohort_summary
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze entry reach and TTL sensitivity.")
    parser.add_argument(
        "--research-root", type=Path,
        default=Path("research_data/runtime_thesis_replay_20260830"),
    )
    args = parser.parse_args()
    root = ROOT / args.research_root
    theses = json.loads((root / "theses.json").read_text(encoding="utf-8"))["theses"]
    manifest = json.loads((root / "demo_1m_manifest.json").read_text(encoding="utf-8"))
    cached = load_cached_1m(manifest)

    rows = []
    for thesis in theses:
        snapshots = thesis.get("rr_snapshots") or []
        if not snapshots:
            continue
        cohorts = [("FIRST_QUALITY_PASS", snapshots[0])]
        first_rr_pass = next(
            (snapshot for snapshot in snapshots if snapshot.get("status") == "PASS"), None
        )
        if first_rr_pass:
            cohorts.append(("FIRST_RR_PASS", first_rr_pass))
        for cohort, snapshot in cohorts:
            inputs = snapshot_plan_inputs(thesis, snapshot)
            # Reach only needs entry + ATR; target-less cases remain measurable.
            if inputs is None:
                target_plan = (snapshot.get("details") or {}).get("target_plan") or {}
                stop_plan = target_plan.get("stop_plan") or {}
                entry = float(target_plan.get("entry") or thesis.get("midpoint") or 0)
                atr = float(stop_plan.get("atr") or thesis.get("atr_at_creation") or 0)
            else:
                entry, atr = inputs["entry"], inputs["atr"]
            if entry <= 0 or atr <= 0:
                continue
            rows.append({
                "cohort": cohort,
                "thesis_id": thesis["thesis_id"],
                "symbol": thesis["symbol"],
                "side": thesis["side"],
                "snapshot_at": snapshot["timestamp"],
                "metrics": reach_metrics(
                    cached.get(thesis["symbol"], ()), at=parse_utc(snapshot["timestamp"]),
                    entry=entry, atr=atr,
                ),
            })
    summary = summarize(rows)
    payload = {
        "schema_version": 1,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "ttl_minutes": TTL_MINUTES,
        "summary": summary,
        "rows": rows,
    }
    output = root / "entry_reach_ttl.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
