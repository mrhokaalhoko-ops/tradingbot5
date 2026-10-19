import gzip
import logging
import os
import shutil
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from core.log_security import redact_secrets


def get_log_dir() -> str:
    explicit = os.getenv("BOT_LOG_DIR", "").strip()
    if explicit:
        # قد تُستخدم نفس .env محلياً وعلى Railway. المسار Linux الخاص بالـ
        # Volume لا يجب أن يتحول على Windows إلى E:\\app\\data بلا صلاحية.
        if os.name == "nt" and explicit.replace("\\", "/").startswith("/app/data"):
            return os.path.abspath("logs")
        return os.path.abspath(explicit)
    data_dir = os.getenv("DATA_DIR", "").strip()
    if data_dir:
        if os.name == "nt" and data_dir.replace("\\", "/") == "/app/data":
            return os.path.abspath("logs")
        return os.path.abspath(os.path.join(data_dir, "logs"))
    if os.name != "nt" and os.path.isdir("/app/data"):
        return "/app/data/logs"
    return os.path.abspath("logs")


def _level(name: str, default: int) -> int:
    return getattr(logging, str(name or "").upper(), default)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = redact_secrets(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


class GZipTimedRotatingFileHandler(TimedRotatingFileHandler):
    """تدوير يومي UTC مع ضغط gzip للملف السابق."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("utc", True)
        super().__init__(*args, **kwargs)
        self.namer = lambda name: f"{name}.gz"
        self.rotator = self._gzip_rotate

    @staticmethod
    def _gzip_rotate(source, dest):
        with open(source, "rb") as src, gzip.open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(source)


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def setup_logger(name="TradingBot"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = UTCFormatter("%(asctime)sZ - %(name)s - %(levelname)s - %(message)s")
    redaction = SecretRedactionFilter()

    compatibility_level = os.getenv("BOT_LOG_LEVEL", "")
    console_level = _level(
        os.getenv("BOT_CONSOLE_LOG_LEVEL", compatibility_level or "INFO"), logging.INFO
    )
    file_level = _level(
        os.getenv("BOT_FILE_LOG_LEVEL", compatibility_level or "DEBUG"), logging.DEBUG
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redaction)
    logger.addHandler(console_handler)

    try:
        log_dir = get_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        retention = max(1, int(os.getenv("BOT_LOG_RETENTION_DAYS", "7")))
        file_handler = GZipTimedRotatingFileHandler(
            os.path.join(log_dir, "bot_activity.log"),
            when="midnight",
            interval=1,
            backupCount=retention,
            encoding="utf-8",
            delay=False,
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        logger.addHandler(file_handler)
    except Exception as exc:
        # لا نسقط البوت بسبب تعذر Volume؛ يبقى Console مع تحذير صريح.
        logger.error(f"[Logger] تعذر تهيئة ملف السجل الدائم: {redact_secrets(exc)}")

    return logger


logger = setup_logger()


def truncate_active_activity_log() -> dict:
    """Truncate the active activity log in place without unlinking an open file."""
    truncated = []
    errors = []
    for handler in list(logger.handlers):
        if not isinstance(handler, GZipTimedRotatingFileHandler):
            continue
        handler.acquire()
        try:
            handler.flush()
            stream = getattr(handler, "stream", None)
            path = Path(handler.baseFilename) if getattr(handler, "baseFilename", None) else None
            before = int(path.stat().st_size) if path and path.exists() else 0
            if stream is None:
                handler.stream = handler._open()
                stream = handler.stream
            stream.flush()
            os.fsync(stream.fileno())
            os.ftruncate(stream.fileno(), 0)
            stream.seek(0)
            truncated.append({"file": str(path) if path else "bot_activity.log", "bytes": before})
        except (OSError, ValueError) as exc:
            errors.append({"file": getattr(handler, "baseFilename", "bot_activity.log"), "error": str(exc)})
        finally:
            handler.release()
    return {"truncated": truncated, "errors": errors}
