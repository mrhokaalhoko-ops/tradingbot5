"""Validate the pre-registered MILD_RISK_ON 24h continuation hypothesis."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from statistics import median


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _metrics(events: list[dict]) -> dict:
    values = [float(event["directional_net_bps"]) for event in events]
    positives = [value for value in values if value > 0]
    return {
        "events": len(values),
        "positive_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "mean_net_bps": sum(values) / len(values) if values else None,
        "median_net_bps": median(values) if values else None,
        "total_net_bps": sum(values),
        "largest_positive_share": (
            max(positives) / sum(positives) if positives else None
        ),
    }


def validate(payload: dict) -> dict:
    method = payload["method"]
    start = _timestamp(method["study_start"])
    end = _timestamp(method["study_end"])
    duration = end - start
    third_boundaries = (start + duration / 3, start + duration * 2 / 3)
    selected = []
    thirds: list[list[dict]] = [[], [], []]
    for event in payload["events"]:
        features = event["features"]
        if (
            event["side"] != "BUY"
            or str(features["global_regime"]) != "MILD_RISK_ON"
            or float(features["return_24h_pct"]) < 10.0
        ):
            continue
        selected.append(event)
        timestamp = _timestamp(event["timestamp"])
        index = 0 if timestamp < third_boundaries[0] else 1 if timestamp < third_boundaries[1] else 2
        thirds[index].append(event)

    overall = _metrics(selected)
    third_metrics = [_metrics(rows) for rows in thirds]
    positive_thirds = sum(
        metrics["mean_net_bps"] is not None and metrics["mean_net_bps"] > 0
        for metrics in third_metrics
    )
    criteria = {
        "minimum_30_events": overall["events"] >= 30,
        "positive_full_sample_mean": (
            overall["mean_net_bps"] is not None and overall["mean_net_bps"] > 0
        ),
        "at_least_two_positive_calendar_thirds": positive_thirds >= 2,
        "largest_winner_below_25pct_of_positive_pnl": (
            overall["largest_positive_share"] is not None
            and overall["largest_positive_share"] < 0.25
        ),
    }
    return {
        "rule": {
            "side": "BUY",
            "minimum_return_24h_pct": 10.0,
            "global_regime": "MILD_RISK_ON",
            "selection": "top 20% cross-sectional 24h return",
            "entry": method["entry"],
            "exit": method["exit"],
            "costs_bps": method["costs_bps"],
        },
        "overall": overall,
        "calendar_thirds": third_metrics,
        "criteria": criteria,
        "passed": all(criteria.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.path.read_text(encoding="utf-8"))
    print(json.dumps(validate(payload), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
