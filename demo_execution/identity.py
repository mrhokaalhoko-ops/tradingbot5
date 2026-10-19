"""Reproducible source identity for the dedicated Demo B executor."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from strategy.prospective_shadow_spec import build_freeze_spec


DEMO_RUNTIME_SOURCE_FILES = (
    "demo_execution/__init__.py",
    "demo_execution/config.py",
    "demo_execution/client.py",
    "demo_execution/cross_environment.py",
    "demo_execution/store.py",
    "demo_execution/executor.py",
    "demo_execution/identity.py",
    "scripts/run_demo_b_r4.py",
    "shadow_runtime/models.py",
    "shadow_runtime/market.py",
    "shadow_runtime/features.py",
    "shadow_runtime/policies.py",
    "strategy/prospective_shadow_spec.py",
)


def demo_runtime_identity(root: str | Path) -> dict:
    root = Path(root)
    commit = (
        os.getenv("DEMO_RUNTIME_COMMIT")
        or os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or ""
    ).strip()
    if not commit:
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            commit = "0" * 40
    manifest = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest().upper()
        for relative in DEMO_RUNTIME_SOURCE_FILES
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    freeze = build_freeze_spec(root)
    policy = next(
        row for row in freeze["policies"]
        if row["policy_id"] == "MOMENTUM_REPRICED_V1"
    )
    return {
        "runtime_commit": commit,
        "runtime_build_hash": hashlib.sha256(encoded).hexdigest().upper(),
        "runtime_source_manifest": manifest,
        "freeze_id": freeze["freeze_id"],
        "freeze_hash": freeze["freeze_hash"],
        "policy_id": policy["policy_id"],
        "policy_version": policy["policy_version"],
        "policy_config_hash": policy["config_hash"],
    }

