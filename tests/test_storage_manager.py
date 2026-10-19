import os
import logging
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import Mock, patch

from utils.storage_manager import (
    cleanup_data_volume,
    get_storage_breakdown,
    get_storage_usage,
    prepare_storage_for_new_test,
)
from utils.telegram_bot import TelegramNotifier


class TestStorageUsage(unittest.TestCase):
    def test_disk_usage_reports_mb_and_percentages(self):
        usage_tuple = namedtuple("usage", "total used free")
        fake = usage_tuple(total=100 * 1024 * 1024, used=25 * 1024 * 1024, free=75 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            result = get_storage_usage(directory, disk_usage_func=lambda _path: fake)

        self.assertEqual(result["total_mb"], 100.0)
        self.assertEqual(result["used_mb"], 25.0)
        self.assertEqual(result["free_mb"], 75.0)
        self.assertEqual(result["used_percent"], 25.0)
        self.assertEqual(result["free_percent"], 75.0)

    def test_breakdown_explains_protected_and_archived_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            (logs / "bot_activity.log").write_bytes(b"a" * 10)
            (logs / "decision_audit.jsonl").write_bytes(b"b" * 20)
            (logs / "bot_activity.log.2026-07-14.gz").write_bytes(b"c" * 30)
            (root / "trades.db").write_bytes(b"d" * 40)

            result = get_storage_breakdown(directory)

        self.assertEqual(result["active_logs"]["bytes"], 30)
        self.assertEqual(result["active_logs"]["files"], 2)
        self.assertEqual(result["archived_logs"]["bytes"], 30)
        self.assertEqual(result["database_and_state"]["bytes"], 40)
        self.assertEqual(result["_totals"]["apparent_bytes"], 100)
        self.assertIn("unaccounted_bytes", result["_totals"])


class TestSafeStorageCleanup(unittest.TestCase):
    @staticmethod
    def _write(path: Path, content: bytes, mtime: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (mtime, mtime))

    def test_cleanup_deletes_only_old_allowlisted_files(self):
        now = time.time()
        old = now - 48 * 3600
        recent = now - 15 * 60
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"

            old_bot_archive = logs / "bot_activity.log.2026-07-10.gz"
            old_audit_archive = logs / "decision_audit.jsonl.2026-07-10.gz"
            recent_archive = logs / "bot_activity.log.2026-07-14.gz"
            old_runtime_zip = root / "tmp" / "runtime_logs_24h_20260710_1200.zip"
            old_runtime_48h_zip = root / "tmp" / "runtime_logs_48h_20260710_1200.zip"
            old_runtime_72h_zip = root / "tmp" / "runtime_logs_72h_20260710_1200.zip"
            old_staging_dir = root / "tmp" / "runtime_logs_72h_abandoned"
            old_staging_payload = old_staging_dir / "bot_activity_72h.log"
            recent_staging_dir = root / "tmp" / "runtime_logs_48h_active"
            recent_staging_payload = recent_staging_dir / "bot_activity_48h.log"
            active_log = logs / "bot_activity.log"
            active_audit = logs / "decision_audit.jsonl"
            database = root / "trades.db"
            database_wal = root / "trades.db-wal"
            state = root / "bot_state.json"
            unrelated_gzip = logs / "important_backup.gz"
            unrelated_zip = root / "manual_export.zip"

            self._write(old_bot_archive, b"old-bot", old)
            self._write(old_audit_archive, b"old-audit", old)
            self._write(recent_archive, b"recent", recent)
            self._write(old_runtime_zip, b"old-zip", old)
            self._write(old_runtime_48h_zip, b"old-48h", old)
            self._write(old_runtime_72h_zip, b"old-72h", old)
            self._write(old_staging_payload, b"abandoned-stage", old)
            self._write(recent_staging_payload, b"active-stage", recent)
            os.utime(old_staging_dir, (old, old))
            os.utime(recent_staging_dir, (recent, recent))
            self._write(active_log, b"active", old)
            self._write(active_audit, b"active-audit", old)
            self._write(database, b"database", old)
            self._write(database_wal, b"wal", old)
            self._write(state, b"state", old)
            self._write(unrelated_gzip, b"important", old)
            self._write(unrelated_zip, b"manual", old)

            result = cleanup_data_volume(
                directory,
                archive_min_age_hours=24,
                temp_zip_min_age_hours=1,
                now_timestamp=now,
            )

            self.assertFalse(old_bot_archive.exists())
            self.assertFalse(old_audit_archive.exists())
            self.assertFalse(old_runtime_zip.exists())
            self.assertFalse(old_runtime_48h_zip.exists())
            self.assertFalse(old_runtime_72h_zip.exists())
            self.assertFalse(old_staging_dir.exists())
            self.assertTrue(recent_staging_dir.exists())
            for protected in (
                recent_archive, active_log, active_audit, database, database_wal,
                state, unrelated_gzip, unrelated_zip,
            ):
                self.assertTrue(protected.exists(), f"يجب عدم حذف {protected.name}")
            self.assertEqual(result["deleted_count"], 6)
            self.assertEqual(
                result["freed_bytes"],
                len(b"old-botold-auditold-zipold-48hold-72habandoned-stage"),
            )
            self.assertEqual(result["errors"], [])
            self.assertGreaterEqual(result["breakdown_after"]["active_logs"]["files"], 2)

    def test_fresh_test_reset_truncates_logs_and_preserves_trading_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            active = logs / "bot_activity.log"
            audit = logs / "decision_audit.jsonl"
            archive = logs / "bot_activity.log.2026-01-01.gz"
            temporary = root / "tmp" / "runtime_logs_72h_old.zip"
            database = root / "trades.db"
            state = root / "bot_state.json"
            journal = root / "trade_journal" / "trade.json"
            for path, payload in (
                (active, b"active-log"), (audit, b"audit-log"), (archive, b"archive"),
                (temporary, b"zip"), (database, b"database"), (state, b"state"),
                (journal, b"journal"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

            def truncate(path):
                before = path.stat().st_size
                path.write_bytes(b"")
                return {"truncated": [{"file": str(path), "bytes": before}], "errors": []}

            with patch.dict(os.environ, {"DATA_DIR": directory}, clear=False):
                result = prepare_storage_for_new_test(
                    directory,
                    activity_truncate_fn=lambda: truncate(active),
                    audit_truncate_fn=lambda: truncate(audit),
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["truncated_count"], 2)
            self.assertFalse(archive.exists())
            self.assertFalse(temporary.exists())
            self.assertEqual(active.stat().st_size, 0)
            self.assertEqual(audit.stat().st_size, 0)
            self.assertEqual(database.read_bytes(), b"database")
            self.assertEqual(state.read_bytes(), b"state")
            self.assertEqual(journal.read_bytes(), b"journal")

    def test_fresh_test_reset_refuses_active_72h_session_without_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            archive = logs / "bot_activity.log.2026-01-01.gz"
            archive.write_bytes(b"archive")
            (root / "72h_test_session.json").write_text(
                '{"status":"RUNNING","test_id":"active-test"}', encoding="utf-8"
            )
            truncate = Mock()
            with patch.dict(os.environ, {"DATA_DIR": directory}, clear=False):
                result = prepare_storage_for_new_test(
                    directory,
                    activity_truncate_fn=truncate,
                    audit_truncate_fn=truncate,
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "ACTIVE_72H_TEST_SESSION")
            self.assertTrue(archive.exists())
            truncate.assert_not_called()

    def test_activity_log_can_be_truncated_in_place_and_continue_writing(self):
        from core import logger as logger_module

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot_activity.log"
            handler = logger_module.GZipTimedRotatingFileHandler(
                str(path), when="midnight", backupCount=1, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord("test", logging.INFO, __file__, 1, "before", (), None)
            handler.emit(record)
            handler.flush()
            self.assertGreater(path.stat().st_size, 0)
            try:
                with patch.object(logger_module.logger, "handlers", [handler]):
                    result = logger_module.truncate_active_activity_log()
                    self.assertEqual(result["errors"], [])
                    self.assertEqual(path.stat().st_size, 0)
                    handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "after", (), None))
                    handler.flush()
                    self.assertIn("after", path.read_text(encoding="utf-8"))
            finally:
                handler.close()


class TestStorageTelegramAuthorization(unittest.TestCase):
    def _notifier(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.chat_id = "123"
        notifier.answer_callback = Mock()
        notifier.edit_message = Mock()
        notifier.ICONS = {}
        return notifier

    def test_storage_action_rejects_different_callback_user(self):
        notifier = self._notifier()
        callback = {
            "id": "storage-cb",
            "data": "storage_status",
            "from": {"id": 999},
            "message": {"chat": {"id": 123}, "message_id": 10},
        }
        with patch.dict(os.environ, {"TELEGRAM_WEBAPP_USER_ID": "123"}, clear=False):
            notifier.handle_callback_query(
                callback, Mock(), Mock(), Mock(), Mock(), Mock(), Mock()
            )

        notifier.answer_callback.assert_called_once_with(
            "storage-cb",
            "إدارة التخزين تتطلب TELEGRAM_WEBAPP_USER_ID مصرحاً وصريحاً.",
            show_alert=True,
        )
        notifier.edit_message.assert_not_called()

    def test_storage_action_rejects_missing_explicit_user_id(self):
        notifier = self._notifier()
        callback = {
            "id": "storage-cb-2",
            "data": "storage_cleanup_request",
            "from": {"id": 123},
            "message": {"chat": {"id": 123}, "message_id": 11},
        }
        with patch.dict(os.environ, {"TELEGRAM_WEBAPP_USER_ID": ""}, clear=False):
            notifier.handle_callback_query(
                callback, Mock(), Mock(), Mock(), Mock(), Mock(), Mock()
            )

        self.assertTrue(notifier.answer_callback.call_args.kwargs["show_alert"])
        notifier.edit_message.assert_not_called()

    def test_fresh_test_reset_refuses_when_tracked_position_is_open(self):
        notifier = self._notifier()
        notifier.send_message = Mock(return_value=True)
        notifier.reports_menu_keyboard = Mock(return_value={})
        state = Mock()
        state.get_active_symbols.return_value = ["BTC/USDT"]

        result = notifier.run_fresh_test_storage_cleanup(state)

        self.assertTrue(result)
        message = notifier.send_message.call_args.args[0]
        self.assertIn("رُفضت تهيئة السجلات", message)
        self.assertIn("BTC/USDT", message)


if __name__ == "__main__":
    unittest.main()
