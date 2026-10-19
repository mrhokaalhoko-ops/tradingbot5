"""Cross-process single-writer lease for the trading account.

The lock lives on the persistent Railway volume and is held by the process file
descriptor.  The operating system releases it automatically after OOM/SIGKILL,
so there is no stale-heartbeat takeover window that could create two leaders.
"""

import json
import os
import socket
import time


class SingleWriterLease:
    def __init__(self, path=None, owner_id=None):
        data_dir = os.getenv("DATA_DIR", "." if os.name == "nt" else "/app/data")
        self.path = path or os.getenv(
            "BOT_SINGLE_WRITER_LOCK_FILE",
            os.path.join(data_dir, "trading_writer.lock"),
        )
        self.owner_id = str(owner_id or os.getenv("RAILWAY_DEPLOYMENT_ID") or os.getpid())
        self._handle = None
        self._locked = False

    def _try_lock(self):
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False

        import fcntl

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    def acquire(self, timeout_seconds=90.0, poll_interval=0.25):
        if self._locked:
            return True
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        self._handle = open(self.path, "a+", encoding="utf-8")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write("\n")
            self._handle.flush()

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            if self._try_lock():
                self._locked = True
                metadata = {
                    "owner_id": self.owner_id,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "acquired_at": time.time(),
                }
                self._handle.seek(0)
                self._handle.truncate()
                self._handle.write(json.dumps(metadata, ensure_ascii=False) + "\n")
                self._handle.flush()
                os.fsync(self._handle.fileno())
                return True
            if time.monotonic() >= deadline:
                self._handle.close()
                self._handle = None
                return False
            time.sleep(max(0.01, float(poll_interval)))

    def release(self):
        if not self._handle:
            self._locked = False
            return
        try:
            if self._locked:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self._handle.close()
            self._handle = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("another trading writer owns the persistent lease")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
