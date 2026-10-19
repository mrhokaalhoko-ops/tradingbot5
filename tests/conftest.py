"""Test-process isolation that must run before importing project modules."""

import atexit
import os
import shutil
import tempfile


_TEST_LOG_DIR = tempfile.mkdtemp(prefix="tradingbot_pytest_logs_")
os.environ["BOT_LOG_DIR"] = _TEST_LOG_DIR
os.environ.setdefault("BOT_CONSOLE_LOG_LEVEL", "CRITICAL")


@atexit.register
def _cleanup_test_logs():
    shutil.rmtree(_TEST_LOG_DIR, ignore_errors=True)
