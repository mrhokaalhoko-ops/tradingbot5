"""Low-overhead process/container diagnostics and crash-session markers."""

import json
import os
import sys
import builtins
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psutil

try:
    import resource
except ImportError:  # Windows development environment
    resource = None

from core.logger import logger


MB = 1024 * 1024
RUN_ID = uuid.uuid4().hex
PROCESS_STARTED_AT = time.time()
_PROCESS = psutil.Process(os.getpid())
_lock = threading.RLock()
_peak_rss_bytes = 0
_last_logged_peak_bytes = 0
_monitor = None


def _observability_context(*, include_storage=False):
    """Best-effort, bounded diagnostics; never make trading depend on them."""
    context = {}
    price_ws = getattr(builtins, "_global_ws_manager", None)
    candle_ws = getattr(builtins, "_global_candle_stream", None)
    for key, manager in (("price_websocket", price_ws), ("candle_websocket", candle_ws)):
        getter = getattr(manager, "get_stats", None)
        if callable(getter):
            try:
                context[key] = getter()
            except Exception as exc:
                context[key] = {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
    if include_storage:
        try:
            from utils.storage_manager import get_storage_breakdown, get_storage_usage

            context["storage"] = {
                "usage": get_storage_usage(),
                "breakdown": get_storage_breakdown(),
            }
        except Exception as exc:
            context["storage"] = {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
    return context


def _audit_resource_snapshot(name, snapshot, details=None, *, include_storage=False):
    try:
        from utils.decision_audit import audit_decision_event

        audit_decision_event(
            stage="RESOURCE_SNAPSHOT",
            status=str(name),
            details={
                "resource": snapshot,
                "milestone_details": details or {},
                **_observability_context(include_storage=include_storage),
            },
        )
    except Exception as exc:
        logger.debug("[MEMORY] تعذر تسجيل resource audit: %s", exc)


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_int_file(*paths):
    for path in paths:
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
            if raw and raw.lower() != "max":
                value = int(raw)
                # cgroup v1 represents "unlimited" with a value close to 2^63.
                if 0 < value < (1 << 60):
                    return value
        except (OSError, ValueError):
            continue
    return None


def _cgroup_memory():
    return {
        "current_bytes": _read_int_file(
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        ),
        "limit_bytes": _read_int_file(
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    }


def _cgroup_memory_events():
    for path in (
        "/sys/fs/cgroup/memory.events",
        "/sys/fs/cgroup/memory/memory.failcnt",
    ):
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if path.endswith("memory.failcnt"):
            try:
                return {"failcnt": int(raw)}
            except ValueError:
                return {}
        values = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    values[parts[0]] = int(parts[1])
                except ValueError:
                    continue
        return values
    return {}


def _platform_peak_rss_bytes():
    if resource is None:
        return 0
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # Linux reports KiB; macOS reports bytes. Railway containers are Linux.
        return value * 1024 if sys.platform.startswith("linux") else value
    except Exception:
        return 0


def resource_snapshot():
    global _peak_rss_bytes
    try:
        memory = _PROCESS.memory_info()
        rss = int(memory.rss)
        vms = int(memory.vms)
        threads = int(_PROCESS.num_threads())
        process_cpu_percent = float(_PROCESS.cpu_percent(interval=None))
        try:
            file_descriptors = int(_PROCESS.num_fds())
        except (AttributeError, psutil.Error):
            try:
                file_descriptors = int(_PROCESS.num_handles())
            except (AttributeError, psutil.Error):
                file_descriptors = None
    except psutil.Error:
        rss = vms = threads = 0
        process_cpu_percent = 0.0
        file_descriptors = None

    with _lock:
        _peak_rss_bytes = max(_peak_rss_bytes, rss, _platform_peak_rss_bytes())
        peak = _peak_rss_bytes
    cgroup = _cgroup_memory()
    cgroup_events = _cgroup_memory_events()
    limit = cgroup["limit_bytes"]
    current = cgroup["current_bytes"]
    return {
        "run_id": RUN_ID,
        "pid": os.getpid(),
        "uptime_seconds": max(0.0, time.time() - PROCESS_STARTED_AT),
        "rss_bytes": rss,
        "rss_mb": rss / MB,
        "peak_rss_bytes": peak,
        "peak_rss_mb": peak / MB,
        "vms_bytes": vms,
        "threads": threads,
        "process_cpu_percent": process_cpu_percent,
        "file_descriptors": file_descriptors,
        "cgroup_current_bytes": current,
        "cgroup_current_mb": (current / MB) if current is not None else None,
        "cgroup_limit_bytes": limit,
        "cgroup_limit_mb": (limit / MB) if limit is not None else None,
        "cgroup_headroom_mb": (
            max(0, limit - current) / MB
            if limit is not None and current is not None else None
        ),
        "cgroup_oom_events": cgroup_events.get("oom"),
        "cgroup_oom_kill_events": cgroup_events.get("oom_kill"),
        "cgroup_memory_failcnt": cgroup_events.get("failcnt"),
    }


def log_memory_milestone(name, *, force=False, details=None):
    global _last_logged_peak_bytes
    snapshot = resource_snapshot()
    step_bytes = max(
        1.0, float(os.getenv("MEMORY_LOG_HIGH_WATER_STEP_MB", "32") or 32)
    ) * MB
    should_log = force or (
        snapshot["peak_rss_bytes"] >= _last_logged_peak_bytes + step_bytes
    )
    if should_log:
        with _lock:
            _last_logged_peak_bytes = max(
                _last_logged_peak_bytes, snapshot["peak_rss_bytes"]
            )
        extra = ""
        if details:
            extra = " " + " ".join(f"{key}={value}" for key, value in details.items())
        logger.info(
            "[MEMORY] %s run=%s rss=%.1fMB peak=%.1fMB threads=%s fds=%s "
            "cgroup=%.1f/%.1fMB%s",
            name,
            RUN_ID[:12],
            snapshot["rss_mb"],
            snapshot["peak_rss_mb"],
            snapshot["threads"],
            snapshot["file_descriptors"],
            snapshot["cgroup_current_mb"] or 0.0,
            snapshot["cgroup_limit_mb"] or 0.0,
            extra,
        )
        _audit_resource_snapshot(
            name,
            snapshot,
            details,
            include_storage=str(name) in {
                "PROCESS_START", "PROCESS_SHUTDOWN", "PERIODIC_RESOURCE",
                "LOG_EXPORT_START", "LOG_EXPORT_END",
            },
        )
    return snapshot


@contextmanager
def memory_operation(name, **details):
    before = log_memory_milestone(f"{name}_START", force=True, details=details)
    started = time.monotonic()
    try:
        yield before
    finally:
        after = log_memory_milestone(
            f"{name}_END",
            force=True,
            details={
                **details,
                "elapsed_s": round(time.monotonic() - started, 3),
                "delta_mb": round(resource_snapshot()["rss_mb"] - before["rss_mb"], 2),
            },
        )


def _runtime_dir():
    configured = os.getenv("DATA_DIR", "").strip()
    if configured and not (
        os.name == "nt" and configured.replace("\\", "/") == "/app/data"
    ):
        return Path(configured)
    return Path("data") if os.name == "nt" else Path("/app/data")


def _session_path():
    return _runtime_dir() / "runtime_session.json"


def _session_history_path(run_id):
    return _session_path().parent / "runtime_sessions" / f"{run_id}.json"


def _prune_session_history(keep=20):
    history_dir = _session_history_path(RUN_ID).parent
    try:
        files = sorted(
            (path for path in history_dir.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[max(2, int(keep)):]:
            if path.stem != RUN_ID:
                path.unlink()
    except OSError as exc:
        logger.debug("[RuntimeSession] تعذر تنظيف تاريخ الجلسات: %s", exc)


def _atomic_json_write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix="runtime_session_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def begin_runtime_session():
    path = _session_path()
    previous = None
    try:
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        previous = None
    previous_running = bool(previous and previous.get("status") == "RUNNING")
    deployment_id = os.getenv("RAILWAY_DEPLOYMENT_ID") or None
    previous_deployment_id = (previous or {}).get("railway_deployment_id")
    deployment_overlap = bool(
        previous_running
        and deployment_id
        and previous_deployment_id
        and deployment_id != previous_deployment_id
    )
    unexpected = bool(previous_running and not deployment_overlap)
    payload = {
        "run_id": RUN_ID,
        "pid": os.getpid(),
        "started_at": _utc_now(),
        "status": "RUNNING",
        "previous_run_id": (previous or {}).get("run_id"),
        "previous_exit_status": (previous or {}).get("status"),
        "previous_exit_reason": (previous or {}).get("exit_reason"),
        "railway_deployment_id": deployment_id,
        "railway_git_commit_sha": os.getenv("RAILWAY_GIT_COMMIT_SHA") or None,
        "startup_resource_snapshot": resource_snapshot(),
    }
    _atomic_json_write(path, payload)
    _atomic_json_write(_session_history_path(RUN_ID), payload)
    _prune_session_history()
    if unexpected:
        logger.warning(
            "[RuntimeSession] UNEXPECTED_PROCESS_RESTART previous_run=%s current_run=%s; "
            "السبب غير مثبت وقد يكون OOM/process kill/deploy قسرياً.",
            (previous or {}).get("run_id"),
            RUN_ID,
        )
    elif deployment_overlap:
        logger.info(
            "[RuntimeSession] DEPLOYMENT_OVERLAP previous_deployment=%s "
            "current_deployment=%s; لا يُصنّف Crash.",
            previous_deployment_id,
            deployment_id,
        )
    else:
        logger.info("[RuntimeSession] PROCESS_START run_id=%s", RUN_ID)
    return {
        "current": payload,
        "previous": previous,
        "unexpected_restart": unexpected,
        "deployment_overlap": deployment_overlap,
    }


def mark_graceful_shutdown(reason="NORMAL"):
    path = _session_path()
    payload = {
        "run_id": RUN_ID,
        "pid": os.getpid(),
        "started_at": datetime.fromtimestamp(
            PROCESS_STARTED_AT, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "ended_at": _utc_now(),
        "status": "GRACEFUL_SHUTDOWN",
        "exit_reason": str(reason),
        "railway_deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID") or None,
        "railway_git_commit_sha": os.getenv("RAILWAY_GIT_COMMIT_SHA") or None,
        "last_resource_snapshot": resource_snapshot(),
    }
    try:
        _atomic_json_write(_session_history_path(RUN_ID), payload)
        # لا يجوز لتشغيل قديم أثناء draining أن يطغى على مؤشر تشغيل أحدث.
        current = None
        try:
            if path.exists():
                current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            current = None
        if not current or current.get("run_id") == RUN_ID:
            _atomic_json_write(path, payload)
        else:
            logger.info(
                "[RuntimeSession] حُفظ إغلاق run=%s في التاريخ دون استبدال "
                "المؤشر الأحدث run=%s.",
                RUN_ID,
                current.get("run_id"),
            )
        return True
    except OSError as exc:
        logger.error("[RuntimeSession] تعذر حفظ shutdown marker: %s", exc)
        return False


class ResourceMonitor:
    def __init__(self):
        self.interval = max(
            0.5, float(os.getenv("MEMORY_SAMPLE_INTERVAL_SECONDS", "2") or 2)
        )
        self.warning_mb = max(
            0.0, float(os.getenv("MEMORY_WARNING_MB", "0") or 0)
        )
        self.warning_cooldown = max(
            60.0, float(os.getenv("MEMORY_WARNING_COOLDOWN_SECONDS", "3600") or 3600)
        )
        self._stop = threading.Event()
        self._thread = None
        self._notifier = None
        self._last_warning = 0.0
        self.audit_interval = max(
            60.0, float(os.getenv("RESOURCE_AUDIT_INTERVAL_SECONDS", "900") or 900)
        )
        self._last_audit = 0.0

    def set_notifier(self, notifier):
        self._notifier = notifier

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="ResourceMonitor", daemon=True
        )
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def _run(self):
        while not self._stop.wait(self.interval):
            snapshot = log_memory_milestone("HIGH_WATER")
            now = time.time()
            if now - self._last_audit >= self.audit_interval:
                self._last_audit = now
                snapshot = log_memory_milestone("PERIODIC_RESOURCE", force=True)
            if not self.warning_mb or snapshot["rss_mb"] < self.warning_mb:
                continue
            if now - self._last_warning < self.warning_cooldown:
                continue
            self._last_warning = now
            logger.critical(
                "[MEMORY] RSS_WARNING rss=%.1fMB threshold=%.1fMB",
                snapshot["rss_mb"], self.warning_mb,
            )
            if self._notifier is not None:
                try:
                    self._notifier.send_message(
                        "🚨 <b>تحذير ذاكرة البوت</b>\n"
                        f"RSS: <code>{snapshot['rss_mb']:.1f} MB</code>\n"
                        f"Peak: <code>{snapshot['peak_rss_mb']:.1f} MB</code>\n"
                        f"Run: <code>{RUN_ID[:12]}</code>"
                    )
                except Exception as exc:
                    logger.warning("[MEMORY] تعذر إرسال التنبيه: %s", exc)


def start_resource_monitor(notifier=None):
    global _monitor
    with _lock:
        if _monitor is None:
            _monitor = ResourceMonitor()
        if notifier is not None:
            _monitor.set_notifier(notifier)
        _monitor.start()
        return _monitor


def stop_resource_monitor():
    with _lock:
        monitor = _monitor
    if monitor is not None:
        monitor.stop()
