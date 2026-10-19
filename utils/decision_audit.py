import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone

from core.log_security import redact_secrets
from core.logger import GZipTimedRotatingFileHandler, get_log_dir, logger
from utils.runtime_health import PROCESS_STARTED_AT, RUN_ID


_write_lock = threading.Lock()
_audit_logger = None
_audit_logger_path = None
_event_sequence = 0


def _json_default(value):
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(value)


def decision_audit_path() -> str:
    return os.path.join(get_log_dir(), "decision_audit.jsonl")


def _get_audit_logger():
    global _audit_logger, _audit_logger_path
    desired_path = os.path.abspath(decision_audit_path())
    if _audit_logger is not None and _audit_logger_path == desired_path:
        return _audit_logger
    if _audit_logger is not None:
        _close_handlers(_audit_logger)
        _audit_logger = None
        _audit_logger_path = None
    audit_logger = logging.getLogger("DecisionAuditJSONL")
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    if not audit_logger.handlers:
        path = desired_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        retention = max(1, int(os.getenv("BOT_LOG_RETENTION_DAYS", "7")))
        handler = GZipTimedRotatingFileHandler(
            path,
            when="midnight",
            interval=1,
            backupCount=retention,
            encoding="utf-8",
            delay=False,
            utc=True,
        )
        handler.suffix = "%Y-%m-%d"
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit_logger.addHandler(handler)
    _audit_logger = audit_logger
    _audit_logger_path = desired_path
    return audit_logger


def audit_decision_event(
    *,
    scan_id=None,
    decision_id=None,
    symbol=None,
    stage,
    status,
    trade_id=None,
    setup_id=None,
    thesis_id=None,
    signal_id=None,
    virtual_order_id=None,
    exchange_order_id=None,
    position_id=None,
    details=None,
    durable=False,
):
    """إضافة حدث قرار واحد بصورة append-only وآمنة بين الخيوط."""
    global _event_sequence
    try:
        with _write_lock:
            # The sequence is process-local by design.  Together with run_id it
            # makes missing/duplicated audit writes objectively detectable.
            _event_sequence += 1
            event = {
                "schema_version": 3,
                "event_id": str(uuid.uuid4()),
                "event_sequence": _event_sequence,
                "run_id": RUN_ID,
                "process_id": os.getpid(),
                "process_started_at": datetime.fromtimestamp(
                    PROCESS_STARTED_AT, tz=timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "scan_id": scan_id,
                "decision_id": decision_id,
                "signal_id": signal_id or decision_id,
                "setup_id": setup_id or thesis_id,
                "thesis_id": thesis_id or setup_id,
                "virtual_order_id": virtual_order_id,
                "exchange_order_id": exchange_order_id,
                "position_id": position_id,
                "trade_id": trade_id,
                "symbol": symbol,
                "stage": str(stage),
                "status": str(status),
                "details": details or {},
            }
            payload = redact_secrets(
                json.dumps(
                    event, ensure_ascii=False, separators=(",", ":"),
                    default=_json_default,
                )
            )
            audit_logger = _get_audit_logger()
            audit_logger.info(payload)
            if durable:
                _flush_handlers(audit_logger, fsync=True)
        return True
    except Exception as exc:
        logger.error(f"[DecisionAudit] فشل تسجيل الحدث {stage}/{status}: {exc}")
        return False


def _flush_handlers(audit_logger, *, fsync=False):
    for handler in audit_logger.handlers:
        handler.flush()
        stream = getattr(handler, "stream", None)
        if fsync and stream is not None:
            os.fsync(stream.fileno())


def _close_handlers(audit_logger):
    for handler in list(audit_logger.handlers):
        try:
            handler.flush()
        finally:
            handler.close()
            audit_logger.removeHandler(handler)


def flush_decision_audit(*, fsync=True) -> bool:
    """Flush the active JSONL stream before evidence export or shutdown."""
    try:
        with _write_lock:
            _flush_handlers(_get_audit_logger(), fsync=fsync)
        return True
    except Exception as exc:
        logger.error(f"[DecisionAudit] تعذر flush: {exc}")
        return False


def close_decision_audit() -> bool:
    """Close the active stream cleanly (tests and graceful process shutdown)."""
    global _audit_logger, _audit_logger_path
    try:
        with _write_lock:
            if _audit_logger is not None:
                _close_handlers(_audit_logger)
            _audit_logger = None
            _audit_logger_path = None
        return True
    except Exception as exc:
        logger.error(f"[DecisionAudit] تعذر إغلاق stream: {exc}")
        return False


def truncate_active_decision_audit() -> dict:
    """Truncate the active JSONL stream in place while holding its write lock."""
    truncated = []
    errors = []
    with _write_lock:
        audit_logger = _get_audit_logger()
        for handler in list(audit_logger.handlers):
            handler.acquire()
            try:
                handler.flush()
                stream = getattr(handler, "stream", None)
                path = getattr(handler, "baseFilename", decision_audit_path())
                before = os.path.getsize(path) if os.path.exists(path) else 0
                if stream is None:
                    handler.stream = handler._open()
                    stream = handler.stream
                stream.flush()
                os.fsync(stream.fileno())
                os.ftruncate(stream.fileno(), 0)
                stream.seek(0)
                truncated.append({"file": str(path), "bytes": int(before)})
            except (OSError, ValueError) as exc:
                errors.append({"file": str(getattr(handler, "baseFilename", "decision_audit.jsonl")), "error": str(exc)})
            finally:
                handler.release()
    return {"truncated": truncated, "errors": errors}
