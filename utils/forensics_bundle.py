"""Build a bounded, secret-free evidence bundle for the 72-hour test cohort."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.config import Config
from core.log_security import _SECRET_ENV_NAMES, redact_secrets
from core.logger import get_log_dir, logger
from utils.decision_audit import flush_decision_audit
from utils.runtime_log_exporter import cleanup_export, export_runtime_logs_zip
from utils.evidence_reconstructor import reconstruct_from_runtime_zips
from utils.test_session import (
    code_identity,
    configuration_hash,
    configuration_snapshot,
    load_test_session,
    sanitized_session_text,
)


@dataclass
class EvidenceBundleResult:
    paths: list
    summary: str
    manifest: dict
    no_data: bool = False
    temp_dir: str = ""
    runtime_export: object = None


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_secrets(text), encoding="utf-8")


def _copy_redacted(source: Path, destination: Path) -> bool:
    try:
        raw = source.read_text(encoding="utf-8", errors="replace")
        _atomic_text(destination, raw)
        return True
    except OSError as exc:
        logger.warning("[EvidenceBundle] تعذر نسخ %s: %s", source, exc)
        return False


def _safe_sqlite_backup(source: Path, destination: Path) -> bool:
    """Consistent online backup followed by redaction of every TEXT cell."""
    if not source.exists() or not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(str(destination), timeout=10)
    try:
        src.backup(dst)
        tables = [
            row[0] for row in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            safe_table = table.replace('"', '""')
            columns = dst.execute(f'PRAGMA table_info("{safe_table}")').fetchall()
            text_columns = [row[1] for row in columns if "TEXT" in str(row[2]).upper()]
            for column in text_columns:
                safe_column = column.replace('"', '""')
                try:
                    rows = dst.execute(
                        f'SELECT rowid, "{safe_column}" FROM "{safe_table}" '
                        f'WHERE "{safe_column}" IS NOT NULL'
                    ).fetchall()
                    for rowid, value in rows:
                        redacted = redact_secrets(value)
                        if redacted != str(value):
                            dst.execute(
                                f'UPDATE "{safe_table}" SET "{safe_column}"=? WHERE rowid=?',
                                (redacted, rowid),
                            )
                except sqlite3.Error:
                    continue
        dst.commit()
    finally:
        src.close()
        dst.close()
    return True


def _known_secrets():
    values = set()
    for name in _SECRET_ENV_NAMES:
        raw = os.getenv(name, "")
        if name.endswith("_API_KEYS"):
            values.update(item.strip() for item in raw.split(",") if item.strip())
        elif raw:
            values.add(raw)
    return {item.encode("utf-8") for item in values if len(item) >= 6}


def _assert_no_known_secret_in_file(path: Path) -> None:
    secrets = _known_secrets()
    if not secrets:
        return

    def inspect_bytes(data: bytes, label: str):
        if any(secret in data for secret in secrets):
            raise RuntimeError(f"secret redaction verification failed in {label}")

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.file_size > 200 * 1024 * 1024:
                    raise RuntimeError(f"oversized unchecked bundle member: {info.filename}")
                inspect_bytes(archive.read(info), f"{path.name}:{info.filename}")
    else:
        inspect_bytes(path.read_bytes(), path.name)


def _within(path: Path, start: datetime, end: datetime) -> bool:
    try:
        changed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return start <= changed <= end
    except OSError:
        return False


def _copy_recent_tree(source: Path, destination: Path, start: datetime, end: datetime):
    copied = []
    if not source.exists():
        return copied
    for path in source.rglob("*"):
        if path.is_symlink() or not path.is_file() or not _within(path, start, end):
            continue
        relative = path.relative_to(source)
        target = destination / relative
        if _copy_redacted(path, target):
            copied.append(target)
    return copied


def _audit_integrity(runtime_paths):
    sequences = {}
    run_ids = set()
    duplicates = []
    for path in runtime_paths:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.startswith("decision_audit_") or not name.endswith(".jsonl"):
                    continue
                for raw in archive.read(name).decode("utf-8", errors="replace").splitlines():
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    run_id = event.get("run_id")
                    sequence = event.get("event_sequence")
                    if run_id:
                        run_ids.add(str(run_id))
                    if run_id and isinstance(sequence, int):
                        values = sequences.setdefault(str(run_id), [])
                        if sequence in values:
                            duplicates.append({"run_id": run_id, "event_sequence": sequence})
                        values.append(sequence)
    gaps = []
    for run_id, values in sequences.items():
        ordered = sorted(set(values))
        for previous, current in zip(ordered, ordered[1:]):
            if current > previous + 1:
                gaps.append({"run_id": run_id, "after": previous, "before": current})
    return {
        "run_ids": sorted(run_ids),
        "sequence_ranges": {
            run_id: {"min": min(values), "max": max(values), "count": len(set(values))}
            for run_id, values in sequences.items() if values
        },
        "sequence_gaps": gaps,
        "duplicate_sequences": duplicates,
        "legacy_events_without_sequence_possible": not bool(sequences),
    }


def _copy_metadata(data_dir: Path, stage: Path, start: datetime, end: datetime):
    copied = []
    simple = {
        data_dir / "bot_state.json": stage / "state" / "bot_state.json",
        data_dir / "runtime_session.json": stage / "runtime" / "runtime_session.json",
        data_dir / "72h_test_session.json": stage / "runtime" / "72h_test_session.json",
        data_dir / "derivatives_audit_log.csv": stage / "derivatives" / "derivatives_audit_log.csv",
        Path(getattr(Config, "TESTNET_TRADES_LOG", data_dir / "testnet_trades_log.csv")):
            stage / "trades" / "testnet_trades_log.csv",
    }
    for source, destination in simple.items():
        if source.exists() and _copy_redacted(source, destination):
            copied.append(destination)
    for folder in ("runtime_sessions", "test_sessions"):
        copied.extend(_copy_recent_tree(data_dir / folder, stage / "runtime" / folder, start, end))
    journal = Path(getattr(Config, "TRADE_JOURNAL_DIR", data_dir / "trade_journal"))
    copied.extend(_copy_recent_tree(journal, stage / "trade_journal", start, end))
    return copied


def export_72h_test_bundle(*, state_manager=None, now=None, data_dir=None, max_zip_bytes=None):
    """Export all durable evidence without changing trading state or thresholds."""
    now = _utc(now)
    session = load_test_session()
    configured_start = session.get("started_at")
    start = _utc(configured_start) if configured_start else now - timedelta(hours=72)
    duration_hours = max(0.0, (now - start).total_seconds() / 3600.0)
    flush_decision_audit(fsync=True)

    runtime = export_runtime_logs_zip(
        hours=72, now=now, start_time=start, max_zip_bytes=max_zip_bytes
    )
    root = Path(runtime.temp_dir) if runtime.temp_dir else Path(tempfile.mkdtemp(prefix="72H_TEST_BUNDLE_"))
    stage = root / "evidence"
    stage.mkdir(parents=True, exist_ok=True)
    data_root = Path(data_dir or getattr(Config, "DATA_DIR", "/app/data")).resolve()

    snapshot = configuration_snapshot(state_manager)
    identity = code_identity()
    _atomic_text(stage / "configuration_snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
    _atomic_text(stage / "code_identity.json", json.dumps(identity, ensure_ascii=False, indent=2))
    _atomic_text(stage / "test_session.json", sanitized_session_text(session))
    _copy_metadata(data_root, stage, start, now)

    database_source = Path(os.getenv("TRADES_DB", str(data_root / "trades.db")))
    database_target = stage / "database" / "trades_sanitized.db"
    database_included = _safe_sqlite_backup(database_source, database_target)

    stamp = now.strftime("%Y%m%d_%H%M")
    test_id = str(session.get("test_id") or "unscheduled")
    runtime_paths = []
    for index, old in enumerate(runtime.paths, start=1):
        new = root / f"72H_TEST_BUNDLE_{test_id}_{stamp}_part{index:02d}.zip"
        os.replace(old, new)
        runtime_paths.append(new)

    integrity = _audit_integrity(runtime_paths)
    reconstruction = reconstruct_from_runtime_zips(runtime_paths)
    _atomic_text(
        stage / "reconstruction_drills.json",
        json.dumps(reconstruction, ensure_ascii=False, indent=2),
    )
    files = [path for path in stage.rglob("*") if path.is_file()]
    artifacts = [
        {
            "path": str(path.relative_to(stage)).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(files)
    ]
    runtime_artifacts = [
        {"path": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in runtime_paths
    ]
    current_config_hash = configuration_hash(snapshot)
    initial_config_hash = session.get("configuration_hash")
    config_drift = bool(initial_config_hash and initial_config_hash != current_config_hash)
    initial_identity = session.get("code_identity") or {}
    initial_commit = initial_identity.get("git_commit")
    current_commit = identity.get("git_commit")
    code_drift = bool(initial_commit and current_commit and initial_commit != current_commit)
    interrupted = session.get("status") == "INTERRUPTED" or config_drift or code_drift
    status = "READY" if (
        duration_hours >= 72 and session.get("status") == "RUNNING" and not interrupted
    ) else ("INTERRUPTED" if interrupted else "PREMATURE")
    drill_status_counts = {}
    for drill in (reconstruction.get("drills") or {}).values():
        drill_status = str(drill.get("status") or "UNKNOWN")
        drill_status_counts[drill_status] = drill_status_counts.get(drill_status, 0) + 1
    manifest = {
        "schema_version": 1,
        "bundle_type": "72H_TEST_BUNDLE",
        "test_id": test_id,
        "status": status,
        "test_started_at": start.isoformat().replace("+00:00", "Z"),
        "exported_at": now.isoformat().replace("+00:00", "Z"),
        "duration_hours": round(duration_hours, 3),
        "code_identity": identity,
        "configuration_hash": current_config_hash,
        "initial_configuration_hash": initial_config_hash,
        "configuration_changed_during_test": bool(session.get("changes")) or config_drift,
        "code_changed_during_test": code_drift,
        "interruptions": session.get("interruptions") or [],
        "audit_integrity": integrity,
        "reconstruction_drills": {
            "status_counts": drill_status_counts,
            "report_path": "reconstruction_drills.json",
            "note": "MISSING_EVIDENCE can mean the scenario did not occur; it is not fabricated as a pass.",
        },
        "database_included": database_included,
        "runtime_parts": runtime_artifacts,
        "evidence_artifacts": artifacts,
        "known_limitations": [
            "Events written before audit schema v3 may not have event_sequence.",
            "SQLite is a consistent export-time snapshot; row history may predate the test window.",
        ],
    }
    _atomic_text(stage / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    evidence_index = len(runtime_paths) + 1
    evidence_zip = root / f"72H_TEST_BUNDLE_{test_id}_{stamp}_part{evidence_index:02d}.zip"
    with zipfile.ZipFile(evidence_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in stage.rglob("*"):
            if path.is_file():
                archive.write(path, str(path.relative_to(stage)).replace("\\", "/"))

    limit = int(max_zip_bytes or os.getenv("TELEGRAM_LOG_ZIP_MAX_BYTES", str(45 * 1024 * 1024)))
    if evidence_zip.stat().st_size > limit:
        raise RuntimeError("evidence metadata part exceeds Telegram size limit")
    paths = runtime_paths + [evidence_zip]
    for path in paths:
        _assert_no_known_secret_in_file(path)

    summary = (
        f"72H evidence status: {status}\n"
        f"Test ID: {test_id}\n"
        f"UTC range: {start.isoformat()} -> {now.isoformat()}\n"
        f"Duration: {duration_hours:.2f} hours\n"
        f"Parts: {len(paths)}\n"
        f"Runs: {len(integrity['run_ids'])}\n"
        f"Sequence gaps: {len(integrity['sequence_gaps'])}\n"
        f"Configuration changed: {'YES' if session.get('changes') else 'NO'}"
    )
    return EvidenceBundleResult(
        paths=[str(path) for path in paths], summary=summary, manifest=manifest,
        no_data=runtime.no_data, temp_dir=str(root), runtime_export=runtime,
    )


def cleanup_evidence_bundle(result):
    if not result:
        return
    runtime = getattr(result, "runtime_export", None)
    root = Path(getattr(result, "temp_dir", "") or "")
    if root and str(root) not in (".", ""):
        shutil.rmtree(root, ignore_errors=True)
    if runtime and runtime.temp_dir and Path(runtime.temp_dir) != root:
        cleanup_export(runtime)
