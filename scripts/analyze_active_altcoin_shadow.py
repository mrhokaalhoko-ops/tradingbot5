"""Summarize prospective Active Altcoin shadow evidence from runtime exports.

This is a research-only reader.  It accepts JSONL files, gzip-compressed JSONL,
ZIP exports, or directories containing any of those formats.  It never imports
the exchange client and cannot create an order.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
from statistics import median
import zipfile


SHADOW_STAGE = "ACTIVE_ALTCOIN_SHADOW"
OUTCOME_PREFIX = "OUTCOME_"
REFERENCE_GATE = {
    "minimum_positions": 100,
    "minimum_profit_factor": 1.10,
    "maximum_drawdown_r": 20.0,
    "require_positive_expectancy": True,
    "require_non_negative_both_sides": True,
}


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iter_lines_from_bytes(payload: bytes, name: str):
    if name.lower().endswith(".gz"):
        payload = gzip.decompress(payload)
    for line in io.StringIO(payload.decode("utf-8-sig", errors="replace")):
        if line.strip():
            yield line


def _iter_source_lines(path: Path):
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if child.is_file() and (
                child.suffix.lower() in {".jsonl", ".gz", ".zip"}
                or child.name.lower().endswith(".jsonl.gz")
            ):
                yield from _iter_source_lines(child)
        return
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for member in sorted(archive.namelist()):
                lowered = member.lower()
                if lowered.endswith(".jsonl") or lowered.endswith(".jsonl.gz"):
                    yield from _iter_lines_from_bytes(archive.read(member), member)
        return
    if path.suffix.lower() == ".gz" or path.name.lower().endswith(".jsonl.gz"):
        with gzip.open(path, "rt", encoding="utf-8-sig", errors="replace") as stream:
            yield from stream
        return
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        yield from stream


def load_shadow_events(paths: list[Path]) -> tuple[list[dict], dict]:
    """Load and de-duplicate ACTIVE_ALTCOIN_SHADOW events."""
    events: list[dict] = []
    seen: set[str] = set()
    diagnostics = Counter()
    for path in paths:
        if not path.exists():
            diagnostics["missing_sources"] += 1
            continue
        diagnostics["sources"] += 1
        for line in _iter_source_lines(path):
            diagnostics["lines"] += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                diagnostics["invalid_json_lines"] += 1
                continue
            if not isinstance(event, dict):
                diagnostics["non_object_lines"] += 1
                continue
            if str(event.get("stage") or "").upper() != SHADOW_STAGE:
                continue
            identity = str(event.get("event_id") or "").strip()
            if not identity:
                identity = hashlib.sha256(
                    json.dumps(event, sort_keys=True, ensure_ascii=False).encode(
                        "utf-8"
                    )
                ).hexdigest()
            if identity in seen:
                diagnostics["duplicates"] += 1
                continue
            seen.add(identity)
            events.append(event)
    events.sort(key=lambda row: str(row.get("timestamp") or ""))
    diagnostics["shadow_events"] = len(events)
    return events, dict(diagnostics)


def _outcome_record(event: dict) -> dict | None:
    status = str(event.get("status") or "").upper()
    if not status.startswith(OUTCOME_PREFIX):
        return None
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    outcome = details.get("outcome") if isinstance(details.get("outcome"), dict) else {}
    plan = details.get("plan") if isinstance(details.get("plan"), dict) else {}
    embedded_event = plan.get("event") if isinstance(plan.get("event"), dict) else {}
    net_r = _finite_float(outcome.get("net_r"))
    if net_r is None:
        net_r = _finite_float(outcome.get("realized_r"))
    side = str(
        outcome.get("side")
        or plan.get("side")
        or embedded_event.get("side")
        or "UNKNOWN"
    ).upper()
    return {
        "timestamp": _parse_timestamp(event.get("timestamp")),
        "symbol": str(event.get("symbol") or plan.get("symbol") or "UNKNOWN").upper(),
        "status": status.removeprefix(OUTCOME_PREFIX),
        "side": side,
        "net_r": net_r,
        "plan_id": event.get("virtual_order_id") or plan.get("plan_id"),
    }


def _performance(records: list[dict]) -> dict:
    measured = [row for row in records if row["net_r"] is not None]
    values = [float(row["net_r"]) for row in measured]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = None
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "positions": len(records),
        "measured_positions": len(measured),
        "expectancy_r": (sum(values) / len(values)) if values else None,
        "median_r": median(values) if values else None,
        "win_rate": (len(wins) / len(values)) if values else None,
        "profit_factor": profit_factor,
        "total_r": sum(values),
        "max_drawdown_r": max_drawdown,
    }


def summarize_shadow_events(
    events: list[dict], *, not_before: datetime | None = None
) -> dict:
    selected = []
    for event in events:
        timestamp = _parse_timestamp(event.get("timestamp"))
        if not_before is not None and (timestamp is None or timestamp < not_before):
            continue
        selected.append(event)
    status_counts = Counter(
        str(event.get("status") or "UNKNOWN").upper() for event in selected
    )
    outcomes = [
        record for event in selected if (record := _outcome_record(event)) is not None
    ]

    # Stable plan ids prevent duplicated terminal writes from inflating results.
    unique_outcomes: list[dict] = []
    seen_plans: set[str] = set()
    duplicate_outcomes = 0
    for record in outcomes:
        identity = str(record.get("plan_id") or "").strip()
        if identity and identity in seen_plans:
            duplicate_outcomes += 1
            continue
        if identity:
            seen_plans.add(identity)
        unique_outcomes.append(record)

    performance = _performance(unique_outcomes)
    side_performance = {
        side: _performance([row for row in unique_outcomes if row["side"] == side])
        for side in ("LONG", "SHORT")
    }
    reference_checks = {
        "sample_size": performance["measured_positions"]
        >= REFERENCE_GATE["minimum_positions"],
        "positive_expectancy": (
            performance["expectancy_r"] is not None and performance["expectancy_r"] > 0
        ),
        "profit_factor": (
            performance["profit_factor"] is not None
            and performance["profit_factor"] > REFERENCE_GATE["minimum_profit_factor"]
        ),
        "max_drawdown": performance["max_drawdown_r"]
        < REFERENCE_GATE["maximum_drawdown_r"],
        "both_sides_non_negative": all(
            side_performance[side]["measured_positions"] > 0
            and side_performance[side]["expectancy_r"] is not None
            and side_performance[side]["expectancy_r"] >= 0
            for side in ("LONG", "SHORT")
        ),
    }
    timestamps = [
        timestamp
        for event in selected
        if (timestamp := _parse_timestamp(event.get("timestamp"))) is not None
    ]
    symbols = Counter(row["symbol"] for row in unique_outcomes)
    return {
        "authority": "RESEARCH_ONLY_NO_EXECUTION_AUTHORITY",
        "period": {
            "start": min(timestamps).isoformat() if timestamps else None,
            "end": max(timestamps).isoformat() if timestamps else None,
            "not_before": not_before.isoformat() if not_before else None,
        },
        "events": len(selected),
        "status_counts": dict(sorted(status_counts.items())),
        "unique_terminal_outcomes": len(unique_outcomes),
        "duplicate_terminal_outcomes": duplicate_outcomes,
        "outcome_status_counts": dict(
            sorted(Counter(row["status"] for row in unique_outcomes).items())
        ),
        "performance": performance,
        "side_performance": side_performance,
        "symbols": dict(symbols.most_common()),
        "reference_gate": {
            "policy": REFERENCE_GATE,
            "checks": reference_checks,
            "eligible_for_human_promotion_review": all(reference_checks.values()),
            "warning": "A pass permits review only; it never enables execution automatically.",
        },
    }


def _json_safe(payload: dict) -> dict:
    """Convert infinities to readable strings for strict JSON consumers."""
    if isinstance(payload, dict):
        return {key: _json_safe(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_json_safe(value) for value in payload]
    if isinstance(payload, float) and not math.isfinite(payload):
        return "Infinity" if payload > 0 else "-Infinity"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument(
        "--not-before",
        help="UTC/ISO cutoff. Events before it are excluded from prospective evidence.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    not_before = _parse_timestamp(args.not_before) if args.not_before else None
    if args.not_before and not_before is None:
        raise SystemExit("--not-before must be a valid ISO timestamp")
    events, diagnostics = load_shadow_events(args.sources)
    report = summarize_shadow_events(events, not_before=not_before)
    report["input_diagnostics"] = diagnostics
    rendered = json.dumps(
        _json_safe(report), indent=2, ensure_ascii=False, allow_nan=False
    )
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        temporary.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
