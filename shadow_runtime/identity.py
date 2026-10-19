"""Reproducible runtime identity, separate from the frozen policy identity."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from strategy.prospective_shadow_spec import POLICY_FREEZE_COMMIT, build_freeze_spec


RUNTIME_SOURCE_FILES = (
    "shadow_runtime/__init__.py",
    "shadow_runtime/models.py",
    "shadow_runtime/identity.py",
    "shadow_runtime/store.py",
    "shadow_runtime/market.py",
    "shadow_runtime/fixtures.py",
    "shadow_runtime/features.py",
    "shadow_runtime/portfolio.py",
    "shadow_runtime/policies.py",
    "shadow_runtime/stream.py",
    "shadow_runtime/engine.py",
    "scripts/run_prospective_shadow_r4.py",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def runtime_identity(root: Path) -> dict:
    root = Path(root)
    commit = (
        os.getenv("SHADOW_RUNTIME_COMMIT")
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
        relative: _sha(root / relative)
        for relative in RUNTIME_SOURCE_FILES
        if (root / relative).is_file()
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    spec = build_freeze_spec(root)
    return {
        "policy_freeze_commit": POLICY_FREEZE_COMMIT,
        "runtime_commit": commit,
        "runtime_build_hash": hashlib.sha256(encoded).hexdigest().upper(),
        "runtime_source_manifest": manifest,
        "freeze_id": spec["freeze_id"],
        "freeze_hash": spec["freeze_hash"],
    }
