"""Railway-only supervisor for the isolated Shadow R4 worker.

It starts the frozen public-data Shadow process and mirrors each newly written
summary heartbeat to stdout.  This exposes non-secret health evidence through
Railway logs without SSH access and without adding an HTTP or order route.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path("/app/data")
DATABASE = DATA_ROOT / "shadow_r4.sqlite3"
OUTPUT = DATA_ROOT / "shadow_runtime_r4"
SUMMARY = OUTPUT / "summary.json"
EVENTS = OUTPUT / "events.jsonl"
ACCEPTED_FREEZE_HASH = "62A4237F1178729BA9E6143D6B7C5FF3FCD7D74C3C7E710E681DA01FA6219D86"


def _file_state(path: Path) -> dict:
    try:
        stat = path.stat()
        return {"path": str(path), "exists": True, "size_bytes": stat.st_size}
    except FileNotFoundError:
        return {"path": str(path), "exists": False, "size_bytes": 0}


def _read_summary() -> dict | None:
    try:
        return json.loads(SUMMARY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _quarantine_mismatched_sample() -> None:
    summary = _read_summary()
    observed = ((summary or {}).get("runtime_identity") or {}).get("freeze_hash")
    if not observed or observed == ACCEPTED_FREEZE_HASH:
        return
    suffix = str(int(time.time()))
    moved = {}
    if DATABASE.exists():
        destination = DATA_ROOT / f"shadow_r4_invalid_{suffix}.sqlite3"
        DATABASE.replace(destination)
        moved["database"] = str(destination)
    if OUTPUT.exists():
        destination = DATA_ROOT / f"shadow_runtime_r4_invalid_{suffix}"
        if destination.exists():
            shutil.rmtree(destination)
        OUTPUT.replace(destination)
        moved["output"] = str(destination)
    print(
        "SHADOW_R4_QUARANTINE "
        + json.dumps(
            {"reason": "FREEZE_HASH_MISMATCH", "observed": observed, "moved": moved},
            sort_keys=True,
        ),
        flush=True,
    )


def main() -> int:
    _quarantine_mismatched_sample()
    command = [
        sys.executable,
        "-u",
        str(ROOT / "scripts" / "run_prospective_shadow_r4.py"),
        "--mode",
        "demo-loop",
        "--database",
        str(DATABASE),
        "--output",
        str(OUTPUT),
        "--duration-seconds",
        "315360000",
        "--poll-seconds",
        "60",
    ]
    child_environment = os.environ.copy()
    child_environment.setdefault(
        "SHADOW_RUNTIME_COMMIT", child_environment.get("RAILWAY_GIT_COMMIT_SHA", "")
    )
    child = subprocess.Popen(command, cwd=ROOT, env=child_environment)

    def stop_child(_signum, _frame):
        if child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop_child)
    signal.signal(signal.SIGINT, stop_child)

    print(
        "SHADOW_R4_SUPERVISOR "
        + json.dumps(
            {
                "status": "STARTED",
                "database": str(DATABASE),
                "events": str(EVENTS),
                "summary": str(SUMMARY),
                "order_route": False,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    last_generated_at = None
    sequence = 0
    while child.poll() is None:
        summary = _read_summary()
        generated_at = (summary or {}).get("generated_at_utc")
        if generated_at and generated_at != last_generated_at:
            last_generated_at = generated_at
            sequence += 1
            evidence = {
                "sequence": sequence,
                "generated_at_utc": generated_at,
                "mode": summary.get("mode"),
                "valid_sample": summary.get("valid_sample"),
                "runtime_identity": summary.get("runtime_identity"),
                "heartbeat": summary.get("heartbeat"),
                "counts": summary.get("counts"),
                "by_policy": summary.get("by_policy"),
                "files": {
                    "database": _file_state(DATABASE),
                    "events": _file_state(EVENTS),
                    "summary": _file_state(SUMMARY),
                },
            }
            print(
                "SHADOW_R4_HEARTBEAT " + json.dumps(evidence, sort_keys=True),
                flush=True,
            )
        time.sleep(2.0)

    return_code = int(child.returncode or 0)
    print(
        "SHADOW_R4_SUPERVISOR "
        + json.dumps({"status": "EXITED", "return_code": return_code}, sort_keys=True),
        flush=True,
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
