import gzip
import json
import logging
import os
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from core.log_security import redact_secrets
from core.logger import GZipTimedRotatingFileHandler
from utils.runtime_log_exporter import (
    ExportResult,
    _build_stream_funnel,
    _new_stream_stats,
    _update_decision_aggregate,
    _update_stream_event_stats,
    cleanup_export,
    export_last_24h_zip,
    export_runtime_logs_zip,
)
from utils.telegram_bot import TelegramNotifier


class TestRuntimeLogRotation(unittest.TestCase):
    def test_daily_rotation_produces_valid_gzip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bot_activity.log")
            handler = GZipTimedRotatingFileHandler(
                path, when="midnight", backupCount=7, encoding="utf-8", utc=True
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord("test", logging.INFO, __file__, 1, "before rollover", (), None)
            handler.emit(record)
            handler.doRollover()
            handler.close()

            rotated = [name for name in os.listdir(directory) if name.endswith(".gz")]
            self.assertEqual(len(rotated), 1)
            with gzip.open(os.path.join(directory, rotated[0]), "rt", encoding="utf-8") as handle:
                self.assertIn("before rollover", handle.read())


class TestRuntimeLogExport(unittest.TestCase):
    def test_candidate_state_is_preserved_in_decision_summary(self):
        aggregated = {}
        _update_decision_aggregate(aggregated, {
            "decision_id": "scan-1:SOLUSDT",
            "stage": "CANDIDATE_STATE",
            "status": "WAITING_FOR_VOLATILITY",
            "details": {
                "gate_class": "SOFT_SETUP",
                "pipeline_progress_pct": 20.0,
                "candidate_score": 61.5,
                "candidate_score_band": "WEAK_SETUP",
                "evidence_coverage_pct": 55.0,
                "next_trigger": "ATR must recover",
            },
        })

        row = aggregated["scan-1:SOLUSDT"]
        self.assertEqual(row["candidate_state"], "WAITING_FOR_VOLATILITY")
        self.assertEqual(row["candidate_gate_class"], "SOFT_SETUP")
        self.assertEqual(row["pipeline_progress_pct"], 20.0)
        self.assertEqual(row["candidate_score"], 61.5)
        self.assertEqual(row["candidate_score_band"], "WEAK_SETUP")
        self.assertEqual(row["evidence_coverage_pct"], 55.0)
        self.assertEqual(row["next_trigger"], "ATR must recover")

    def test_momentum_shadow_funnel_is_counted_separately_from_live_trades(self):
        stats = _new_stream_stats()
        events = [
            {
                "event_id": "mc-1", "stage": "MOMENTUM_CONTINUATION",
                "status": "SETUP_CREATED", "setup_id": "setup-1", "details": {},
            },
            {
                "event_id": "mc-2", "stage": "MOMENTUM_CONTINUATION",
                "status": "CONFIRMATION_PASS", "setup_id": "setup-1", "details": {},
            },
            {
                "event_id": "mc-3", "stage": "MOMENTUM_CONTINUATION",
                "status": "PLAN_QUALIFIED_SHADOW", "setup_id": "setup-1",
                "virtual_order_id": "plan-1",
                "details": {"plan": {"plan_id": "plan-1"}},
            },
            {
                "event_id": "mc-4", "stage": "MOMENTUM_CONTINUATION",
                "status": "OUTCOME_TARGET_FIRST", "setup_id": "setup-1",
                "virtual_order_id": "plan-1",
                "details": {
                    "plan": {"plan_id": "plan-1"},
                    "outcome": {"final_r": 1.8},
                },
            },
        ]
        for event in events:
            _update_stream_event_stats(stats, event)

        funnel = _build_stream_funnel(stats)
        self.assertEqual(funnel["momentum_shadow_setups"], 1)
        self.assertEqual(funnel["momentum_shadow_confirmations"], 1)
        self.assertEqual(funnel["momentum_shadow_qualified_plans"], 1)
        self.assertEqual(funnel["momentum_shadow_terminal_plans"], 1)
        self.assertEqual(funnel["momentum_shadow_average_final_r"], 1.8)
        self.assertEqual(funnel["executed_positions"], 0)

    def test_paired_smc_shadow_setup_is_visible_in_export_funnel(self):
        stats = _new_stream_stats()
        events = [
            {
                "event_id": "event-paired",
                "stage": "MOMENTUM_CONTINUATION",
                "status": "SETUP_CREATED_EXTERNAL_SMC",
                "setup_id": "mc_smc_setup_1",
                "details": {
                    "execution_enabled": False,
                    "setup": {"source": "SMC_RR_PASS_PAIRED_SHADOW"},
                },
            },
            {
                "event_id": "event-paired-confirm",
                "stage": "MOMENTUM_CONTINUATION",
                "status": "CONFIRMATION_PASS",
                "setup_id": "mc_smc_setup_1",
                "details": {"setup": {"source": "SMC_RR_PASS_PAIRED_SHADOW"}},
            },
            {
                "event_id": "event-paired-plan",
                "stage": "MOMENTUM_CONTINUATION",
                "status": "PLAN_QUALIFIED_SHADOW",
                "setup_id": "mc_smc_setup_1",
                "details": {
                    "plan": {
                        "plan_id": "mc_smc_plan_1",
                        "source": "SMC_RR_PASS_PAIRED_SHADOW",
                    }
                },
            },
            {
                "event_id": "event-paired-outcome",
                "stage": "MOMENTUM_CONTINUATION",
                "status": "OUTCOME_TARGET_FIRST",
                "setup_id": "mc_smc_setup_1",
                "details": {
                    "plan": {
                        "plan_id": "mc_smc_plan_1",
                        "source": "SMC_RR_PASS_PAIRED_SHADOW",
                    },
                    "outcome": {"final_r": 1.6},
                },
            },
        ]
        for event in events:
            _update_stream_event_stats(stats, event)

        funnel = _build_stream_funnel(stats)
        self.assertEqual(funnel["momentum_shadow_setups"], 1)
        self.assertEqual(funnel["momentum_paired_smc_setups"], 1)
        self.assertEqual(funnel["momentum_paired_smc_confirmations"], 1)
        self.assertEqual(funnel["momentum_paired_smc_qualified_plans"], 1)
        self.assertEqual(funnel["momentum_paired_smc_terminal_plans"], 1)
        self.assertEqual(funnel["momentum_paired_smc_average_final_r"], 1.6)
        self.assertEqual(funnel["executed_positions"], 0)

    def test_export_excludes_lines_older_than_24_hours(self):
        now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
        recent = now - timedelta(hours=2)
        old = now - timedelta(hours=25)
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "bot_activity.log"), "w", encoding="utf-8") as handle:
                handle.write(f"{old.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]}Z - TradingBot - INFO - OLD_LINE\n")
                handle.write(f"{recent.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]}Z - TradingBot - INFO - RECENT_LINE\n")

            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                self.assertFalse(result.no_data)
                with zipfile.ZipFile(result.paths[0]) as archive:
                    payload = archive.read("bot_activity_24h.log").decode("utf-8")
                self.assertIn("RECENT_LINE", payload)
                self.assertNotIn("OLD_LINE", payload)
            finally:
                cleanup_export(result)

    def test_export_reads_previous_gzip_when_window_crosses_midnight(self):
        now = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
        recent = now - timedelta(hours=5)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bot_activity.log.2026-07-13.gz")
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(f"{recent.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]}Z - TradingBot - WARNING - ROTATED_RECENT\n")

            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                with zipfile.ZipFile(result.paths[0]) as archive:
                    payload = archive.read("bot_activity_24h.log").decode("utf-8")
                self.assertIn("ROTATED_RECENT", payload)
                self.assertIn("Warning log lines: 1", result.summary)
            finally:
                cleanup_export(result)

    def test_empty_directory_returns_explicit_no_data(self):
        with tempfile.TemporaryDirectory() as directory:
            result = export_last_24h_zip(log_dir=directory)
        self.assertTrue(result.no_data)
        self.assertEqual(result.paths, [])
        self.assertIn("لا توجد سجلات", result.summary)

    def test_export_contains_raw_and_aggregated_decision_audit(self):
        now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            events = [
                {
                    "timestamp": (now - timedelta(minutes=2)).isoformat(),
                    "scan_id": "scan-1", "decision_id": "scan-1:BTCUSDT",
                    "symbol": "BTC/USDT", "stage": "HYBRID_DECISION", "status": "BUY",
                    "trade_id": "trade-1", "details": {
                        "technical_signal": "BUY", "htf_trend": "bullish",
                        "sentiment_source": "lunarcrush", "sentiment_result": "positive",
                    },
                },
                {
                    "timestamp": (now - timedelta(minutes=1)).isoformat(),
                    "scan_id": "scan-1", "decision_id": "scan-1:BTCUSDT",
                    "symbol": "BTC/USDT", "stage": "EXECUTION", "status": "EXECUTED",
                    "trade_id": "trade-1", "details": {},
                },
            ]
            with open(os.path.join(directory, "decision_audit.jsonl"), "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")

            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                with zipfile.ZipFile(result.paths[0]) as archive:
                    self.assertIn("decision_audit_24h.jsonl", archive.namelist())
                    self.assertIn("funnel_summary.json", archive.namelist())
                    summary_row = json.loads(
                        archive.read("decision_summary_24h.jsonl").decode("utf-8").strip()
                    )
                    funnel = json.loads(archive.read("funnel_summary.json"))
                self.assertEqual(summary_row["15m_signal"], "BUY")
                self.assertEqual(summary_row["final_outcome"], "EXECUTED")
                self.assertEqual(funnel["raw_signals"], 1)
                self.assertEqual(funnel["executed_positions"], 1)
            finally:
                cleanup_export(result)

    def test_signal_without_terminal_event_has_no_final_outcome(self):
        now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
        event = {
            "timestamp": (now - timedelta(minutes=1)).isoformat(),
            "scan_id": "scan-1", "decision_id": "scan-1:BTCUSDT",
            "symbol": "BTC/USDT", "stage": "HYBRID_DECISION",
            "status": "BUY", "trade_id": "trade-1",
            "details": {"technical_signal": "BUY", "htf_trend": "bullish"},
        }
        with tempfile.TemporaryDirectory() as directory:
            with open(
                os.path.join(directory, "decision_audit.jsonl"),
                "w", encoding="utf-8",
            ) as handle:
                handle.write(json.dumps(event) + "\n")
            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                with zipfile.ZipFile(result.paths[0]) as archive:
                    row = json.loads(
                        archive.read("decision_summary_24h.jsonl")
                        .decode("utf-8").strip()
                    )
                self.assertEqual(row["15m_signal"], "BUY")
                self.assertIsNone(row["final_outcome"])
            finally:
                cleanup_export(result)

    def test_export_sorts_active_and_rotated_logs_chronologically(self):
        now = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
        older = now - timedelta(hours=2)
        newer = now - timedelta(minutes=15)
        with tempfile.TemporaryDirectory() as directory:
            rotated = os.path.join(directory, "bot_activity.log.2026-07-14.gz")
            with gzip.open(rotated, "wt", encoding="utf-8") as handle:
                handle.write(
                    f"{older.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]}Z - TradingBot - INFO - OLDER\n"
                )
            with open(os.path.join(directory, "bot_activity.log"), "w", encoding="utf-8") as handle:
                handle.write(
                    f"{newer.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]}Z - TradingBot - INFO - NEWER\n"
                )

            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                with zipfile.ZipFile(result.paths[0]) as archive:
                    payload = archive.read("bot_activity_24h.log").decode("utf-8")
                self.assertLess(payload.index("OLDER"), payload.index("NEWER"))
            finally:
                cleanup_export(result)

    def test_cross_midnight_audit_uses_latest_terminal_event(self):
        now = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
        decision_id = "scan-1:BTCUSDT"
        older_event = {
            "timestamp": (now - timedelta(hours=2)).isoformat(),
            "event_id": "old-event",
            "scan_id": "scan-1",
            "decision_id": decision_id,
            "symbol": "BTC/USDT",
            "stage": "SNIPER",
            "status": "PENDING",
            "trade_id": "trade-1",
            "details": {},
        }
        newer_event = {
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
            "event_id": "new-event",
            "scan_id": "scan-1",
            "decision_id": decision_id,
            "symbol": "BTC/USDT",
            "stage": "SNIPER",
            "status": "EXPIRED_VIRTUAL_TIMEOUT",
            "trade_id": "trade-1",
            "details": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            rotated = os.path.join(directory, "decision_audit.jsonl.2026-07-14.gz")
            with gzip.open(rotated, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(older_event) + "\n")
            with open(os.path.join(directory, "decision_audit.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps(newer_event) + "\n")

            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                with zipfile.ZipFile(result.paths[0]) as archive:
                    row = json.loads(
                        archive.read("decision_summary_24h.jsonl").decode("utf-8").strip()
                    )
                self.assertEqual(row["final_outcome"], "EXPIRED_VIRTUAL_TIMEOUT")
            finally:
                cleanup_export(result)

    def test_summary_counts_one_trade_across_multiple_execution_events(self):
        now = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
        base = {
            "timestamp": (now - timedelta(minutes=1)).isoformat(),
            "scan_id": "scan-1",
            "decision_id": "scan-1:BTCUSDT",
            "symbol": "BTC/USDT",
            "trade_id": "trade-1",
            "details": {},
        }
        events = [
            dict(base, event_id="execution", stage="EXECUTION", status="EXECUTED"),
            dict(base, event_id="final", stage="FINAL_OUTCOME", status="EXECUTED"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "decision_audit.jsonl"), "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")

            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                self.assertIn("Executed trades: 1", result.summary)
            finally:
                cleanup_export(result)

    def test_funnel_counts_recycled_signals_as_one_setup(self):
        now = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
        events = []
        for index in (1, 2):
            decision_id = f"scan-{index}:BTCUSDT"
            events.extend([
                {
                    "event_id": f"signal-{index}",
                    "timestamp": (now - timedelta(minutes=5 - index)).isoformat(),
                    "scan_id": f"scan-{index}", "decision_id": decision_id,
                    "signal_id": decision_id, "setup_id": "setup-shared",
                    "thesis_id": "setup-shared", "symbol": "BTC/USDT",
                    "stage": "HYBRID_DECISION", "status": "BUY", "details": {},
                },
                {
                    "event_id": f"setup-{index}",
                    "timestamp": (now - timedelta(minutes=4 - index)).isoformat(),
                    "scan_id": f"scan-{index}", "decision_id": decision_id,
                    "signal_id": decision_id, "setup_id": "setup-shared",
                    "thesis_id": "setup-shared", "symbol": "BTC/USDT",
                    "stage": "SETUP_CREATED", "status": "ACTIVE", "details": {},
                },
            ])
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "decision_audit.jsonl"), "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")
            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                self.assertIn("Technical signals (BUY/SELL): 2", result.summary)
                self.assertIn("Independent setup theses: 1", result.summary)
                with zipfile.ZipFile(result.paths[0]) as archive:
                    funnel = json.loads(archive.read("funnel_summary.json"))
                self.assertEqual(funnel["raw_signals"], 2)
                self.assertEqual(funnel["independent_setups"], 1)
            finally:
                cleanup_export(result)

    def test_final_outcome_without_trade_id_does_not_double_execution_count(self):
        now = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
        events = [
            {
                "timestamp": (now - timedelta(minutes=2)).isoformat(),
                "scan_id": "scan-1", "decision_id": "scan-1:BTCUSDT",
                "symbol": "BTC/USDT", "stage": "EXECUTION", "status": "EXECUTED",
                "trade_id": "trade-1", "details": {},
            },
            {
                "timestamp": (now - timedelta(minutes=1)).isoformat(),
                "scan_id": "scan-1", "decision_id": "scan-1:BTCUSDT",
                "symbol": "BTC/USDT", "stage": "FINAL_OUTCOME", "status": "EXECUTED",
                "trade_id": None, "details": {},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "decision_audit.jsonl"), "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")
            result = export_last_24h_zip(log_dir=directory, now=now)
            try:
                self.assertIn("Executed trades: 1", result.summary)
            finally:
                cleanup_export(result)

    def test_configurable_24_48_72_hour_windows(self):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        samples = {
            "AGE_23H": now - timedelta(hours=23),
            "AGE_47H": now - timedelta(hours=47),
            "AGE_71H": now - timedelta(hours=71),
            "AGE_73H": now - timedelta(hours=73),
        }
        expected = {
            24: {"AGE_23H"},
            48: {"AGE_23H", "AGE_47H"},
            72: {"AGE_23H", "AGE_47H", "AGE_71H"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bot_activity.log")
            with open(path, "w", encoding="utf-8") as handle:
                for label, timestamp in samples.items():
                    handle.write(
                        f"{timestamp.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]}Z "
                        f"- TradingBot - INFO - {label}\n"
                    )

            for hours, included in expected.items():
                with self.subTest(hours=hours):
                    result = export_runtime_logs_zip(
                        hours=hours,
                        log_dir=directory,
                        now=now,
                    )
                    try:
                        self.assertIn(f"runtime_logs_{hours}h_", os.path.basename(result.paths[0]))
                        with zipfile.ZipFile(result.paths[0]) as archive:
                            payload = archive.read(f"bot_activity_{hours}h.log").decode("utf-8")
                        for label in samples:
                            self.assertEqual(label in payload, label in included)
                        self.assertIn(f"Last {hours} Hours", result.summary)
                    finally:
                        cleanup_export(result)

    def test_custom_minute_window_uses_exact_cutoff_and_minute_names(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bot_activity.log")
            with open(path, "w", encoding="utf-8") as handle:
                for label, timestamp in (
                    ("INSIDE_29M", now - timedelta(minutes=29)),
                    ("OUTSIDE_31M", now - timedelta(minutes=31)),
                ):
                    handle.write(
                        f"{timestamp.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]}Z "
                        f"- TradingBot - INFO - {label}\n"
                    )

            result = export_runtime_logs_zip(
                minutes=30,
                log_dir=directory,
                now=now,
            )
            try:
                self.assertIn("runtime_logs_30m_", os.path.basename(result.paths[0]))
                with zipfile.ZipFile(result.paths[0]) as archive:
                    payload = archive.read("bot_activity_30m.log").decode("utf-8")
                    self.assertIn("decision_audit_30m.jsonl", archive.namelist())
                    self.assertIn("decision_summary_30m.jsonl", archive.namelist())
                self.assertIn("INSIDE_29M", payload)
                self.assertNotIn("OUTSIDE_31M", payload)
                self.assertIn("Last 30 Minutes", result.summary)
            finally:
                cleanup_export(result)

    def test_export_rejects_window_beyond_retention_safety_limit(self):
        with self.assertRaises(ValueError):
            export_runtime_logs_zip(hours=169)


class TestLogSecretRedaction(unittest.TestCase):
    def test_known_and_contextual_secrets_never_pass(self):
        api_key = "ABCD1234567890SECRETKEYVALUE"
        telegram_token = "REDACTED_TEST_TELEGRAM_TOKEN"
        with patch.dict(os.environ, {
            "BINANCE_API_KEY": api_key,
            "TELEGRAM_BOT_TOKEN": telegram_token,
        }, clear=False):
            safe = redact_secrets(
                f"key={api_key} Authorization: Bearer SUPERSECRETTOKEN123456789 "
                f"url=https://api.telegram.org/bot{telegram_token}/sendMessage"
            )

        self.assertNotIn(api_key, safe)
        self.assertNotIn(telegram_token, safe)
        self.assertNotIn("SUPERSECRETTOKEN123456789", safe)
        self.assertIn("ABCD****", safe)


class TestRuntimeLogTelegramAuthorization(unittest.TestCase):
    def test_unauthorized_callback_is_rejected_before_export(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.chat_id = "123"
        notifier.answer_callback = Mock()
        notifier.send_runtime_logs = Mock()

        notifier.handle_callback_query(
            {"id": "cb1", "data": "export_runtime_logs_24h", "message": {
                "chat": {"id": 999}, "message_id": 1
            }},
            client=Mock(), state_manager=Mock(), ai_engine=Mock(), ta_engine=Mock(),
            news_engine=Mock(), social_engine=Mock(),
        )

        notifier.answer_callback.assert_called_once_with(
            "cb1", "غير مصرح لك باستخدام هذا البوت.", show_alert=True
        )
        notifier.send_runtime_logs.assert_not_called()

    def test_empty_logs_send_clear_message_instead_of_error(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.send_message = Mock(return_value=True)
        no_data = ExportResult(paths=[], summary="لا توجد سجلات متاحة", no_data=True)

        with patch("utils.runtime_log_exporter.export_runtime_logs_zip", return_value=no_data):
            self.assertFalse(notifier.send_runtime_logs_last_24h())

        notifier.send_message.assert_called_once()
        self.assertIn("لا توجد سجلات", notifier.send_message.call_args.args[0])

    def test_reports_menu_contains_all_runtime_log_windows(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.ICONS = {}
        callbacks = {
            button["callback_data"]
            for row in notifier.reports_menu_keyboard()["inline_keyboard"]
            for button in row
        }
        self.assertTrue({
            "export_runtime_logs_1h",
            "export_runtime_logs_24h",
            "export_runtime_logs_48h",
            "export_runtime_logs_72h",
            "export_runtime_logs_custom_minutes",
        }.issubset(callbacks))

    def test_hour_callbacks_dispatch_requested_window(self):
        for hours in (1, 48, 72):
            with self.subTest(hours=hours):
                notifier = TelegramNotifier.__new__(TelegramNotifier)
                notifier.chat_id = "123"
                notifier.answer_callback = Mock()
                notifier.edit_message = Mock()
                notifier.back_home_keyboard = Mock(return_value={})
                notifier.send_runtime_logs = Mock()
                callback = {
                    "id": f"cb-{hours}",
                    "data": f"export_runtime_logs_{hours}h",
                    "from": {"id": 123},
                    "message": {"chat": {"id": 123}, "message_id": 10},
                }

                with patch.dict(os.environ, {"TELEGRAM_WEBAPP_USER_ID": "123"}):
                    with patch("utils.telegram_bot.threading.Thread") as thread_cls:
                        notifier.handle_callback_query(
                            callback,
                            client=Mock(), state_manager=Mock(), ai_engine=Mock(),
                            ta_engine=Mock(), news_engine=Mock(), social_engine=Mock(),
                        )

                self.assertEqual(thread_cls.call_args.kwargs["args"], (hours,))
                self.assertEqual(
                    thread_cls.call_args.kwargs["name"],
                    f"runtime-log-export-{hours}h",
                )
                thread_cls.return_value.start.assert_called_once()

    def test_custom_minutes_callback_prompts_for_numeric_input(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.chat_id = "123"
        notifier.user_state = {}
        notifier.answer_callback = Mock()
        notifier.edit_message = Mock()
        notifier.back_home_keyboard = Mock(return_value={})
        callback = {
            "id": "cb-minutes",
            "data": "export_runtime_logs_custom_minutes",
            "from": {"id": 123},
            "message": {"chat": {"id": 123}, "message_id": 10},
        }

        with patch.dict(os.environ, {"TELEGRAM_WEBAPP_USER_ID": "123"}):
            notifier.handle_callback_query(
                callback,
                client=Mock(), state_manager=Mock(), ai_engine=Mock(),
                ta_engine=Mock(), news_engine=Mock(), social_engine=Mock(),
            )

        self.assertEqual(
            notifier.user_state["123"], "awaiting_runtime_log_minutes"
        )
        self.assertIn("بالدقائق", notifier.edit_message.call_args.args[2])

    def test_custom_minutes_text_dispatches_exact_requested_window(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.user_state = {"123": "awaiting_runtime_log_minutes"}
        notifier.send_message = Mock(return_value=True)
        notifier.back_home_keyboard = Mock(return_value={})
        notifier.send_runtime_logs = Mock()

        with patch("utils.telegram_bot.threading.Thread") as thread_cls:
            notifier.handle_text_message(
                "30", 123,
                client=Mock(), state_manager=Mock(), ai_engine=Mock(),
                ta_engine=Mock(), news_engine=Mock(), social_engine=Mock(),
            )

        self.assertNotIn("123", notifier.user_state)
        self.assertEqual(thread_cls.call_args.kwargs["kwargs"], {"minutes": 30})
        self.assertEqual(
            thread_cls.call_args.kwargs["name"], "runtime-log-export-30m"
        )
        thread_cls.return_value.start.assert_called_once()

    def test_72_hour_export_sends_every_generated_part(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.send_message = Mock(return_value=True)
        notifier.send_document = Mock(return_value=True)
        generated = ExportResult(
            paths=["part01.zip", "part02.zip", "part03.zip"],
            summary="72h summary",
        )
        with patch(
            "utils.runtime_log_exporter.export_runtime_logs_zip",
            return_value=generated,
        ) as exporter, patch("utils.runtime_log_exporter.cleanup_export"):
            self.assertTrue(notifier.send_runtime_logs(72))

        exporter.assert_called_once_with(hours=72)
        self.assertEqual(notifier.send_document.call_count, 3)
        captions = [call.kwargs["caption"] for call in notifier.send_document.call_args_list]
        self.assertIn("(1/3)", captions[0])
        self.assertIn("(3/3)", captions[-1])

    def test_custom_minute_export_sends_exact_window_caption(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.send_message = Mock(return_value=True)
        notifier.send_document = Mock(return_value=True)
        generated = ExportResult(paths=["runtime_logs_30m.zip"], summary="30m")
        with patch(
            "utils.runtime_log_exporter.export_runtime_logs_zip",
            return_value=generated,
        ) as exporter, patch("utils.runtime_log_exporter.cleanup_export"):
            self.assertTrue(notifier.send_runtime_logs(minutes=30))

        exporter.assert_called_once_with(hours=1, minutes=30)
        self.assertIn(
            "30 دقيقة", notifier.send_document.call_args.kwargs["caption"]
        )

    def test_parallel_window_requests_are_serialized(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.send_message = Mock(return_value=True)
        notifier.send_document = Mock(return_value=True)
        guard = threading.Lock()
        active = 0
        maximum = 0

        def fake_export(hours):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return ExportResult(paths=[], summary=f"{hours}h")

        with patch(
            "utils.runtime_log_exporter.export_runtime_logs_zip",
            side_effect=fake_export,
        ), patch("utils.runtime_log_exporter.cleanup_export"):
            threads = [
                threading.Thread(target=notifier.send_runtime_logs, args=(hours,))
                for hours in (24, 48, 72)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
