"""Reproducible identity for the Telegram-proposal Demo runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess


RUNTIME_SOURCE_FILES = (
    "main.py",
    "core/config.py",
    "core/exchange.py",
    "core/version.py",
    "strategy/user_visible_trade_proposal.py",
    "strategy/risk_manager.py",
    "strategy/trailing_stop.py",
    "strategy/filter_profiles.py",
    "strategy/entry_confirmation.py",
    "utils/telegram_bot.py",
    "utils/health_monitor.py",
    "utils/operational_snapshot.py",
    "utils/runtime_log_exporter.py",
    "utils/telegram_proposal_runtime_identity.py",
    "utils/state_manager.py",
    "utils/trade_journal.py",
    "utils/json_normalizer.py",
    "webapp/server.py",
    "webapp/templates/dashboard.html",
    "requirements.txt",
    "Dockerfile",
    "railway.json",
)


def _sha256(path: Path) -> str:
    data = path.read_bytes()
    # Git checkouts use CRLF on the Windows development host and LF inside the
    # Railway Linux image.  Hash the canonical source content so the expected
    # local build hash must equal the authoritative container build hash.
    if path.suffix.lower() in {".py", ".txt", ".json", ".toml", ".yaml", ".yml"} or path.name in {
        "Dockerfile", "Procfile"
    }:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest().upper()


def telegram_proposal_runtime_identity(root: str | Path | None = None) -> dict:
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    manifest = {
        relative: _sha256(root / relative)
        for relative in RUNTIME_SOURCE_FILES
        if (root / relative).is_file()
    }
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    commit = str(
        os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or os.getenv("SOURCE_COMMIT")
        or ""
    ).strip()
    if not commit:
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            commit = "0" * 40
    return {
        "runtime_commit": commit,
        "runtime_build_hash": hashlib.sha256(encoded).hexdigest().upper(),
        "runtime_source_manifest": manifest,
    }
