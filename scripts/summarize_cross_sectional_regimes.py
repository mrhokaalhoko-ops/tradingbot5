"""Summarize frozen cross-sectional event outputs by actual market regime."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from statistics import median


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _split_name(timestamp: datetime, start: datetime, end: datetime) -> str:
    duration = end - start
    if timestamp < start + duration * 0.60:
        return "TRAIN"
    if timestamp < start + duration * 0.80:
        return "VALIDATION"
    return "TEST"


def summarize(rows: list[dict]) -> dict:
    returns = [float(row["directional_net_bps"]) for row in rows]
    return {
        "events": len(rows),
        "positive_rate": sum(value > 0 for value in returns) / len(returns) if returns else None,
        "mean_net_bps": sum(returns) / len(returns) if returns else None,
        "median_net_bps": median(returns) if returns else None,
    }


def analyze(path: Path, minimum_abs_24h_pct: float) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    method = payload["method"]
    start = _timestamp(method["study_start"])
    end = _timestamp(method["study_end"])
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for event in payload["events"]:
        features = event["features"]
        if abs(float(features["return_24h_pct"])) < minimum_abs_24h_pct:
            continue
        split = _split_name(_timestamp(event["timestamp"]), start, end)
        grouped[(split, str(features["global_regime"]), str(event["side"]))].append(event)
    return {
        "source": str(path),
        "minimum_abs_24h_pct": minimum_abs_24h_pct,
        "groups": {
            "|".join(key): summarize(rows)
            for key, rows in sorted(grouped.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--minimum-abs-24h-pct", type=float, default=10.0)
    args = parser.parse_args()
    reports = [analyze(path, args.minimum_abs_24h_pct) for path in args.paths]
    print(json.dumps(reports, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
