import json
import os
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core.config import Config
from utils import decision_audit
from utils.evidence_reconstructor import DRILLS, reconstruct_drills
from utils.forensics_bundle import cleanup_evidence_bundle, export_72h_test_bundle
from utils.storage_manager import collect_cleanup_candidates
from utils.test_session import (
    complete_test_session,
    load_test_session,
    record_config_change,
    start_test_session,
)
from utils.telegram_bot import TelegramNotifier


class FakeState:
    def __init__(self):
        self.values = {
            "filter_profile": "smc_balanced",
            "scan_interval_seconds": 900,
            "sentiment_mode": "ta_only",
            "ema_mode": "dual",
        }

    def get(self, key, default=None):
        return self.values.get(key, default)

    def get_risk_pct(self):
        return 0.25

    def get_leverage(self):
        return 5

    def get_max_open_trades(self):
        return 2


def test_decision_audit_has_process_sequence_and_json_safe_values():
    sink = Mock()
    sink.handlers = []
    with patch.object(decision_audit, "_get_audit_logger", return_value=sink), patch.object(
        decision_audit, "_event_sequence", 0
    ):
        assert decision_audit.audit_decision_event(
            stage="TEST", status="ONE", details={"when": datetime.now(timezone.utc)}
        )
        assert decision_audit.audit_decision_event(stage="TEST", status="TWO")
    payloads = [json.loads(call.args[0]) for call in sink.info.call_args_list]
    assert [item["event_sequence"] for item in payloads] == [1, 2]
    assert all(item["schema_version"] == 3 for item in payloads)
    assert all(item["process_started_at"] for item in payloads)


def test_decision_audit_can_be_truncated_in_place_and_continue_writing():
    with tempfile.TemporaryDirectory() as directory, patch.dict(
        os.environ, {"BOT_LOG_DIR": directory}, clear=False
    ):
        decision_audit.close_decision_audit()
        assert decision_audit.audit_decision_event(stage="BEFORE_RESET", status="WRITTEN", durable=True)
        path = Path(directory) / "decision_audit.jsonl"
        assert path.stat().st_size > 0
        result = decision_audit.truncate_active_decision_audit()
        assert result["errors"] == []
        assert path.stat().st_size == 0
        assert decision_audit.audit_decision_event(stage="AFTER_RESET", status="WRITTEN", durable=True)
        assert "AFTER_RESET" in path.read_text(encoding="utf-8")
        decision_audit.close_decision_audit()


def test_test_session_freezes_identity_and_marks_config_change_interrupted():
    state = FakeState()
    with tempfile.TemporaryDirectory() as directory, patch.dict(
        os.environ, {"DATA_DIR": directory}, clear=False
    ), patch("utils.decision_audit.audit_decision_event", return_value=True):
        session = start_test_session(
            state, now=datetime(2026, 8, 21, tzinfo=timezone.utc), force_new=True
        )
        assert session["status"] == "RUNNING"
        assert session["configuration"]["risk_percent"] == 0.25
        record_config_change("leverage", 5, 10, state_manager=state)
        saved = load_test_session()
    assert saved["status"] == "INTERRUPTED"
    assert saved["changes"][0]["setting"] == "leverage"


def test_completed_delivery_closes_session_and_allows_a_new_cohort():
    state = FakeState()
    with tempfile.TemporaryDirectory() as directory, patch.dict(
        os.environ, {"DATA_DIR": directory}, clear=False
    ), patch("utils.decision_audit.audit_decision_event", return_value=True):
        first = start_test_session(state, force_new=True)
        completed = complete_test_session(first["test_id"])
        second = start_test_session(state)
    assert completed["status"] == "COMPLETED"
    assert completed["ended_at"]
    assert second["status"] == "RUNNING"
    assert second["test_id"] != first["test_id"]


def test_cleanup_preserves_archives_inside_active_72h_test_window():
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "logs" / "bot_activity.log.2026-08-20.gz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"evidence")
        age_48h = now.timestamp() - 48 * 3600
        os.utime(archive, (age_48h, age_48h))
        session = {
            "status": "RUNNING",
            "started_at": (now - timedelta(hours=60)).isoformat(),
        }
        with patch("utils.test_session.load_test_session", return_value=session):
            candidates = collect_cleanup_candidates(
                directory, archive_min_age_hours=24, now_timestamp=now.timestamp()
            )
    assert archive.resolve() not in candidates


def _event(stage, status, *, decision="decision-1", trade="trade-1", sequence=1):
    return {
        "schema_version": 3,
        "event_id": f"event-{sequence}",
        "event_sequence": sequence,
        "run_id": "run-1",
        "timestamp": f"2026-08-21T00:{sequence:02d}:00Z",
        "decision_id": decision,
        "setup_id": "setup-1",
        "virtual_order_id": "virtual-1",
        "trade_id": trade,
        "stage": stage,
        "status": status,
        "details": {},
    }


def test_reconstruction_drills_cover_a_through_h_from_artifact_events():
    specifications = {
        "A_QUALITY_REJECTION": [("MARKET_CONTEXT", "CAPTURED"), ("QUALITY_FILTER", "REJECTED"), ("FINAL_OUTCOME", "REJECTED")],
        "B_CONFIRMATION_WAIT": [("ENTRY_ZONE_TOUCH", "TOUCHED"), ("ENTRY_CONFIRMATION", "WAIT")],
        "C_VIRTUAL_INVALIDATION": [("INVALIDATION_STATE", "SWEEP_PENDING"), ("INVALIDATION_STATE", "HARD_INVALIDATED"), ("SNIPER", "CANCELLED")],
        "D_CONFIRMATION_TO_EXECUTION": [("ENTRY_CONFIRMATION", "PASS"), ("EXECUTION_TIMELINE", "ORDER_INTENT_PERSISTED"), ("EXECUTION_TIMELINE", "ENTRY_ORDER_ACKNOWLEDGED"), ("EXECUTION_TIMELINE", "PROTECTION_RESULT"), ("EXECUTION", "EXECUTED")],
        "E_TRAILING_TO_CLOSE": [("TRAILING_STOP", "ACTIVATED"), ("TRAILING_STOP", "SL_REPLACED"), ("POSITION_CLOSED", "TAKE_PROFIT")],
        "F_MANUAL_CLOSE": [("POSITION_CLOSED", "MANUAL_CLOSE")],
        "G_RESTART_RECOVERY": [("RUN_SESSION", "STARTED"), ("STARTUP_RECOVERY", "RECOVERY_COMPLETE")],
        "H_WS_STALE_REST_FALLBACK": [("RESOURCE_SNAPSHOT", "WEBSOCKET_DISCONNECT"), ("PRICE_SOURCE", "REST_FALLBACK")],
    }
    events = []
    sequence = 1
    for index, (_, rows) in enumerate(specifications.items(), start=1):
        for stage, status in rows:
            event = _event(stage, status, decision=f"decision-{index}", trade=f"trade-{index}", sequence=sequence)
            event["run_id"] = f"run-{index}" if stage in ("RUN_SESSION", "STARTUP_RECOVERY", "RESOURCE_SNAPSHOT", "PRICE_SOURCE") else "run-common"
            events.append(event)
            sequence += 1
    report = reconstruct_drills(events)
    assert set(report["drills"]) == set(DRILLS)
    assert all(row["status"] == "RECONSTRUCTABLE" for row in report["drills"].values())


def test_bundle_contains_manifest_database_journal_and_no_known_secret():
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    secret = "test-secret-value-1234567890"
    state = FakeState()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        logs = root / "logs"
        logs.mkdir()
        recent = now - timedelta(minutes=5)
        (logs / "bot_activity.log").write_text(
            f"{recent.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]}Z - TradingBot - INFO - token={secret}\n",
            encoding="utf-8",
        )
        event = _event("RUN_SESSION", "STARTED")
        event["timestamp"] = recent.isoformat()
        (logs / "decision_audit.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        (root / "bot_state.json").write_text(json.dumps({"api_key": secret}), encoding="utf-8")
        journal = root / "trade_journal" / "trade-1"
        journal.mkdir(parents=True)
        journal_entry = journal / "trade_entry.json"
        journal_entry.write_text(json.dumps({"token": secret}), encoding="utf-8")
        # Keep the fixture inside the explicit historical window. Without this,
        # the test becomes clock-dependent once wall time passes ``now`` below.
        os.utime(journal_entry, (recent.timestamp(), recent.timestamp()))
        database = root / "trades.db"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO notes(note) VALUES (?)", (f"secret={secret}",))
        connection.commit()
        connection.close()
        (root / "72h_test_session.json").write_text(json.dumps({
            "test_id": "test72_fixture", "status": "RUNNING",
            "started_at": (now - timedelta(hours=73)).isoformat(),
            "changes": [], "interruptions": [],
        }), encoding="utf-8")

        with patch.dict(os.environ, {
            "DATA_DIR": directory,
            "BOT_LOG_DIR": str(logs),
            "TRADES_DB": str(database),
            "BINANCE_API_KEY": secret,
        }, clear=False), patch.object(Config, "DATA_DIR", directory), patch.object(
            Config, "TRADE_JOURNAL_DIR", str(root / "trade_journal")
        ), patch.object(Config, "TESTNET_TRADES_LOG", str(root / "testnet_trades_log.csv")):
            result = export_72h_test_bundle(
                state_manager=state, now=now, data_dir=directory,
                max_zip_bytes=5 * 1024 * 1024,
            )
            try:
                assert result.manifest["status"] == "READY"
                assert result.manifest["database_included"] is True
                assert all(Path(path).name.startswith("72H_TEST_BUNDLE_") for path in result.paths)
                evidence_path = Path(result.paths[-1])
                with zipfile.ZipFile(evidence_path) as archive:
                    names = set(archive.namelist())
                    assert "manifest.json" in names
                    assert "database/trades_sanitized.db" in names
                    assert "trade_journal/trade-1/trade_entry.json" in names
                    assert "reconstruction_drills.json" in names
                    extracted = root / "sanitized.db"
                    extracted.write_bytes(archive.read("database/trades_sanitized.db"))
                    state_payload = archive.read("state/bot_state.json")
                check = sqlite3.connect(extracted)
                note = check.execute("SELECT note FROM notes").fetchone()[0]
                check.close()
                assert secret not in note
                assert secret.encode() not in state_payload
            finally:
                cleanup_evidence_bundle(result)
                decision_audit.close_decision_audit()


def test_72h_admin_action_requires_explicit_user_id():
    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.chat_id = "123"
    notifier.answer_callback = Mock()
    notifier.edit_message = Mock()
    notifier.ICONS = {}
    callback = {
        "id": "test72-cb", "data": "start_72h_test_session",
        "from": {"id": 123},
        "message": {"chat": {"id": 123}, "message_id": 1},
    }
    with patch.dict(os.environ, {"TELEGRAM_WEBAPP_USER_ID": ""}, clear=False):
        notifier.handle_callback_query(callback, Mock(), Mock(), Mock(), Mock(), Mock(), Mock())
    assert notifier.answer_callback.call_args.kwargs["show_alert"] is True
    notifier.edit_message.assert_not_called()
