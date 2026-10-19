import os
import shutil
import threading
import time
from pathlib import Path

from core.logger import get_log_dir, logger


MB = 1024 * 1024
_cleanup_lock = threading.RLock()
_ARCHIVE_PATTERNS = (
    "bot_activity.log.*.gz",
    "decision_audit.jsonl.*.gz",
)
_TEMP_ZIP_PATTERNS = (
    "runtime_logs_24h_*.zip",
    "runtime_logs_48h_*.zip",
    "runtime_logs_72h_*.zip",
    "72H_TEST_BUNDLE_*.zip",
    "candles_report_*.zip",
)
_STALE_EXPORT_DIR_PREFIXES = (
    "runtime_logs_24h_",
    "runtime_logs_48h_",
    "runtime_logs_72h_",
)


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def resolve_data_dir(data_dir=None) -> Path:
    if data_dir:
        return Path(data_dir).expanduser().resolve()
    configured = os.getenv("DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.path.isdir("/app/data"):
        return Path("/app/data").resolve()
    return Path("data").resolve()


def get_storage_usage(data_dir=None, disk_usage_func=None) -> dict:
    """قياس نظام الملفات الذي يستضيف DATA_DIR دون تقديرات تخمينية."""
    root = resolve_data_dir(data_dir)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"مجلد التخزين غير موجود: {root}")
    usage = (disk_usage_func or shutil.disk_usage)(str(root))
    total = int(usage.total)
    used = int(usage.used)
    free = int(usage.free)
    return {
        "path": str(root),
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "total_mb": total / MB,
        "used_mb": used / MB,
        "free_mb": free / MB,
        "used_percent": (used / total * 100.0) if total else 0.0,
        "free_percent": (free / total * 100.0) if total else 0.0,
    }


def get_storage_breakdown(data_dir=None) -> dict:
    """تفصيل ملفات DATA_DIR حتى يكون واضحاً لماذا قد لا يحرر التنظيف مساحة."""
    root = resolve_data_dir(data_dir)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"مجلد التخزين غير موجود: {root}")

    log_root = Path(get_log_dir()).resolve() if data_dir is None else (root / "logs").resolve()
    buckets = {
        "active_logs": {"files": 0, "bytes": 0},
        "archived_logs": {"files": 0, "bytes": 0},
        "temporary_exports": {"files": 0, "bytes": 0},
        "database_and_state": {"files": 0, "bytes": 0},
        "trade_journal": {"files": 0, "bytes": 0},
        "other": {"files": 0, "bytes": 0},
    }

    apparent_total = 0
    allocated_total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file() or not _inside(path, root):
            continue
        resolved = path.resolve()
        relative = resolved.relative_to(root)
        name = resolved.name
        bucket = "other"

        if resolved.parent == log_root and name in ("bot_activity.log", "decision_audit.jsonl"):
            bucket = "active_logs"
        elif resolved.parent == log_root and any(resolved.match(pattern) for pattern in _ARCHIVE_PATTERNS):
            bucket = "archived_logs"
        elif (
            any(resolved.match(pattern) for pattern in _TEMP_ZIP_PATTERNS)
            or any(part.startswith(_STALE_EXPORT_DIR_PREFIXES) for part in relative.parts)
        ):
            bucket = "temporary_exports"
        elif (
            name == "bot_state.json"
            or name.endswith((".db", ".db-wal", ".db-shm"))
        ):
            bucket = "database_and_state"
        elif relative.parts and relative.parts[0] == "trade_journal":
            bucket = "trade_journal"

        size = _file_size(resolved)
        apparent_total += size
        try:
            allocated_total += int(getattr(resolved.stat(), "st_blocks", 0) or 0) * 512
        except OSError:
            pass
        buckets[bucket]["files"] += 1
        buckets[bucket]["bytes"] += size

    for values in buckets.values():
        values["mb"] = values["bytes"] / MB
    deleted_open_bytes = 0
    proc_fd = Path("/proc/self/fd")
    if proc_fd.exists():
        for fd_path in proc_fd.iterdir():
            try:
                target = os.readlink(fd_path)
                if " (deleted)" in target:
                    deleted_open_bytes += int(fd_path.stat().st_size)
            except OSError:
                continue
    disk_used = get_storage_usage(root)["used_bytes"]
    buckets["_totals"] = {
        "apparent_bytes": apparent_total,
        "apparent_mb": apparent_total / MB,
        "allocated_bytes": allocated_total,
        "allocated_mb": allocated_total / MB,
        "deleted_open_bytes": deleted_open_bytes,
        "deleted_open_mb": deleted_open_bytes / MB,
        "disk_used_bytes": disk_used,
        "disk_used_mb": disk_used / MB,
        "unaccounted_bytes": max(0, disk_used - apparent_total - deleted_open_bytes),
        "unaccounted_mb": max(0, disk_used - apparent_total - deleted_open_bytes) / MB,
    }
    return buckets


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _eligible(path: Path, root: Path, cutoff_timestamp: float) -> bool:
    if path.is_symlink() or not path.is_file() or not _inside(path, root):
        return False
    try:
        return path.stat().st_mtime <= cutoff_timestamp
    except OSError:
        return False


def _eligible_export_dir(path: Path, root: Path, cutoff_timestamp: float) -> bool:
    """قبول مجلد staging معروف فقط، وليس أي مجلد قديم داخل Volume."""
    if path.is_symlink() or not path.is_dir() or not _inside(path, root):
        return False
    if path.parent.resolve() != (root / "tmp").resolve():
        return False
    if not path.name.startswith(_STALE_EXPORT_DIR_PREFIXES):
        return False
    try:
        return path.stat().st_mtime <= cutoff_timestamp
    except OSError:
        return False


def _tree_apparent_size(path: Path, root: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_symlink() or not child.is_file() or not _inside(child, root):
            continue
        total += _file_size(child)
    return total


def collect_cleanup_candidates(
    data_dir=None,
    archive_min_age_hours=None,
    temp_zip_min_age_hours=None,
    now_timestamp=None,
) -> list:
    """إرجاع قائمة allowlist فقط؛ لا تستخدم امتداداً عاماً مثل *.gz أو *.zip."""
    root = resolve_data_dir(data_dir)
    now = float(now_timestamp if now_timestamp is not None else time.time())
    archive_age = float(
        archive_min_age_hours
        if archive_min_age_hours is not None
        else os.getenv("BOT_STORAGE_ARCHIVE_MIN_AGE_HOURS", "96")
    )
    zip_age = float(
        temp_zip_min_age_hours
        if temp_zip_min_age_hours is not None
        else os.getenv("BOT_STORAGE_TEMP_ZIP_MIN_AGE_HOURS", "1")
    )
    log_root = Path(get_log_dir()).resolve() if data_dir is None else (root / "logs").resolve()
    candidates = set()

    # لا ننظف log_dir إذا كان خارج DATA_DIR؛ هذه الميزة مخصصة للـVolume فقط.
    if log_root.exists() and _inside(log_root, root):
        cutoff = now - archive_age * 3600
        # A running/interrupted 72h evidence cohort owns every archive created
        # since its start.  Manual cleanup must never punch a hole in it.
        try:
            from utils.test_session import load_test_session

            session = load_test_session()
            if session.get("status") in ("RUNNING", "INTERRUPTED"):
                started = str(session.get("started_at") or "").replace("Z", "+00:00")
                if started:
                    from datetime import datetime

                    session_cutoff = datetime.fromisoformat(started).timestamp()
                    cutoff = min(cutoff, session_cutoff)
        except (ImportError, OSError, TypeError, ValueError):
            # The fixed 96h default remains the conservative fallback.
            pass
        for pattern in _ARCHIVE_PATTERNS:
            for path in log_root.glob(pattern):
                if _eligible(path, root, cutoff):
                    candidates.add(path.resolve())

    zip_cutoff = now - zip_age * 3600
    # الملفات المؤقتة مسموحة فقط في جذر DATA_DIR أو مجلدي logs/tmp؛ لا
    # نتجول داخل trade_journal أو أي مجلد بيانات آخر.
    for pattern in _TEMP_ZIP_PATTERNS:
        for path in root.glob(pattern):
            if _eligible(path, root, zip_cutoff):
                candidates.add(path.resolve())
    for safe_root in (log_root, root / "tmp"):
        if not safe_root.exists() or not _inside(safe_root, root):
            continue
        for pattern in _TEMP_ZIP_PATTERNS:
            for path in safe_root.rglob(pattern):
                if _eligible(path, root, zip_cutoff):
                    candidates.add(path.resolve())

    staging_root = (root / "tmp").resolve()
    if staging_root.exists() and _inside(staging_root, root):
        for path in staging_root.iterdir():
            if _eligible_export_dir(path, root, zip_cutoff):
                candidates.add(path.resolve())

    return sorted(candidates, key=lambda path: str(path))


def cleanup_data_volume(
    data_dir=None,
    archive_min_age_hours=None,
    temp_zip_min_age_hours=None,
    now_timestamp=None,
) -> dict:
    """حذف ملفات allowlist القديمة فقط وإرجاع تقرير قابل للعرض في Telegram."""
    root = resolve_data_dir(data_dir)
    before = get_storage_usage(root)
    deleted = []
    errors = []
    freed_bytes = 0

    with _cleanup_lock:
        candidates = collect_cleanup_candidates(
            data_dir,
            archive_min_age_hours=archive_min_age_hours,
            temp_zip_min_age_hours=temp_zip_min_age_hours,
            now_timestamp=now_timestamp,
        )
        for path in candidates:
            # إعادة التحقق داخل القفل مباشرة قبل unlink لمقاومة تبديل المسار/Symlink.
            is_stale_export_dir = _eligible_export_dir(path, root, float("inf"))
            if path.is_symlink() or not _inside(path, root) or not (
                path.is_file() or is_stale_export_dir
            ):
                continue
            try:
                size = (
                    _tree_apparent_size(path, root)
                    if is_stale_export_dir
                    else path.stat().st_size
                )
                relative = str(path.relative_to(root))
                if is_stale_export_dir:
                    shutil.rmtree(path)
                else:
                    path.unlink()
                freed_bytes += size
                deleted.append(relative)
            except OSError as exc:
                errors.append({"file": str(path), "error": str(exc)})
                logger.warning(f"[StorageCleanup] تعذر حذف {path.name}: {exc}")

    after = get_storage_usage(root)
    breakdown_after = get_storage_breakdown(root)
    logger.info(
        f"[StorageCleanup] deleted={len(deleted)} freed_mb={freed_bytes / MB:.2f} "
        f"errors={len(errors)}"
    )
    return {
        "path": str(root),
        "deleted_count": len(deleted),
        "deleted_files": deleted,
        "freed_bytes": freed_bytes,
        "freed_mb": freed_bytes / MB,
        "errors": errors,
        "before": before,
        "after": after,
        "breakdown_after": breakdown_after,
    }


def prepare_storage_for_new_test(
    data_dir=None,
    *,
    activity_truncate_fn=None,
    audit_truncate_fn=None,
) -> dict:
    """Clear only log/export evidence after verifying no active 72h cohort exists."""
    root = resolve_data_dir(data_dir)
    from utils.test_session import load_test_session

    session = load_test_session()
    if session.get("status") in ("RUNNING", "INTERRUPTED"):
        return {
            "ok": False,
            "reason": "ACTIVE_72H_TEST_SESSION",
            "test_id": session.get("test_id"),
        }

    if activity_truncate_fn is None:
        from core.logger import truncate_active_activity_log
        activity_truncate_fn = truncate_active_activity_log
    if audit_truncate_fn is None:
        from utils.decision_audit import truncate_active_decision_audit
        audit_truncate_fn = truncate_active_decision_audit

    with _cleanup_lock:
        before = get_storage_usage(root)
        # Zero age is intentional here: this explicit, double-confirmed action
        # prepares a new cohort and deletes only the existing allowlist.
        cleanup = cleanup_data_volume(
            root,
            archive_min_age_hours=0,
            temp_zip_min_age_hours=0,
        )
        activity = activity_truncate_fn() or {}
        audit = audit_truncate_fn() or {}
        errors = list(cleanup.get("errors") or [])
        errors.extend(activity.get("errors") or [])
        errors.extend(audit.get("errors") or [])
        truncated = list(activity.get("truncated") or []) + list(audit.get("truncated") or [])
        truncated_bytes = sum(int(item.get("bytes") or 0) for item in truncated)
        after = get_storage_usage(root)
        breakdown_after = get_storage_breakdown(root)
        apparent_freed = int(cleanup.get("freed_bytes") or 0) + truncated_bytes
        disk_delta = max(0, int(before["used_bytes"]) - int(after["used_bytes"]))

    logger.info(
        "[StorageReset] deleted=%s truncated=%s apparent_freed_mb=%.2f disk_delta_mb=%.2f errors=%s",
        cleanup.get("deleted_count", 0), len(truncated), apparent_freed / MB,
        disk_delta / MB, len(errors),
    )
    return {
        "ok": not errors,
        "reason": "COMPLETED" if not errors else "COMPLETED_WITH_ERRORS",
        "deleted_count": int(cleanup.get("deleted_count") or 0),
        "deleted_files": cleanup.get("deleted_files") or [],
        "truncated_count": len(truncated),
        "truncated_files": truncated,
        "apparent_freed_bytes": apparent_freed,
        "apparent_freed_mb": apparent_freed / MB,
        "disk_delta_bytes": disk_delta,
        "disk_delta_mb": disk_delta / MB,
        "errors": errors,
        "before": before,
        "after": after,
        "breakdown_after": breakdown_after,
    }
