"""CLI for the read-only RR-candidate runtime replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Running ``python scripts/replay_runtime_candidates.py`` sets sys.path to the
# scripts directory, not the repository root. Make the documented CLI work
# without requiring callers to modify PYTHONPATH.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.runtime_candidate_replay import (
    load_decision_events,
    replay_rr_candidates,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay RR-pass candidates from split runtime ZIP exports."
    )
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--horizon-hours", type=float, default=4.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = replay_rr_candidates(
        load_decision_events(args.archives),
        horizon_hours=args.horizon_hours,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
