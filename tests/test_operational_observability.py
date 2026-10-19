import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import main as main_module
from utils.operational_snapshot import (
    build_operational_snapshot,
    flatten_proposal_timelines,
)
from utils.runtime_log_exporter import cleanup_export, export_runtime_logs_zip


class MemoryState:
    def __init__(self, state=None):
        self.state = state or {}

    def get(self, key, default=None):
        return self.state.get(key, default)

    def set(self, key, value):
        self.state[key] = value
        return True

    def get_state(self):
        return json.loads(json.dumps(self.state))


def proposal():
    return {
        "proposal_id": "proposal-1",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "timestamp": "2026-10-11T00:00:00Z",
        "proposed_entry": 100.0,
        "original_stop": 95.0,
        "original_target": 120.0,
        "api_secret": "MUST_NOT_EXPORT",
    }


class TestOperationalObservability(unittest.TestCase):
    def test_proposal_ledger_keeps_bounded_stage_timeline(self):
        state = MemoryState({"user_visible_trade_proposals": {}})
        item = proposal()

        main_module._proposal_ledger_update(
            state, item, "VISIBLE", visible_at="2026-10-11T00:00:00Z"
        )
        main_module._proposal_ledger_update(
            state,
            item,
            "ENTRY_FILLED_AND_PROTECTED",
            entry_post_count=1,
            actual_entry=100.1,
            filled_amount=0.2,
            sl=95.0,
            tp=120.0,
        )

        record = state.state["user_visible_trade_proposals"]["proposal-1"]
        self.assertEqual(
            [event["status"] for event in record["timeline"]],
            ["VISIBLE", "ENTRY_FILLED_AND_PROTECTED"],
        )
        self.assertEqual(record["timeline"][-1]["details"]["entry_post_count"], 1)

    @patch("utils.operational_snapshot.telegram_proposal_runtime_identity")
    def test_snapshot_is_secret_free_and_flags_unprotected_positions(self, identity):
        identity.return_value = {"runtime_build_hash": "A" * 64}
        state = MemoryState({
            "schema_version": 3,
            "recovery_complete": True,
            "last_successful_reconciliation_at": "2026-10-11T00:01:00Z",
            "user_visible_trade_proposals": {
                "proposal-1": {
                    "proposal": proposal(),
                    "status": "VISIBLE",
                    "entry_post_count": 0,
                    "timeline": [{
                        "timestamp": "2026-10-11T00:00:00Z",
                        "status": "VISIBLE",
                        "details": {},
                    }],
                }
            },
            "entry_metadata": {
                "BTC/USDT": {
                    "side": "BUY",
                    "entry_price": 100,
                    "amount": 0.2,
                    "sl": 95,
                    "tp": 120,
                    "protection_status": "UNPROTECTED",
                    "api_key": "MUST_NOT_EXPORT",
                }
            },
        })
        client = Mock()
        client._environment_proof = {
            "is_demo": True,
            "mainnet": False,
            "allowed_host": "demo-fapi.binance.com",
            "observed_hosts": ["demo-fapi.binance.com"],
            "order_calls_possible": True,
        }

        snapshot = build_operational_snapshot(state, client=client)
        encoded = json.dumps(snapshot)

        self.assertNotIn("MUST_NOT_EXPORT", encoded)
        self.assertEqual(snapshot["endpoint"]["mainnet"], False)
        self.assertEqual(
            snapshot["protection_summary"]["unprotected_managed_symbols"],
            ["BTC/USDT"],
        )
        self.assertIn("MANAGED_POSITION_NOT_PROVEN_PROTECTED", snapshot["warnings"])
        rows = flatten_proposal_timelines(snapshot)
        self.assertEqual(rows[0]["proposal_id"], "proposal-1")
        self.assertEqual(rows[0]["status"], "VISIBLE")

    @patch("utils.operational_snapshot.telegram_proposal_runtime_identity")
    def test_runtime_zip_contains_review_ready_operational_artifacts(self, identity):
        identity.return_value = {"runtime_build_hash": "B" * 64}
        now = datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)
        state = MemoryState({
            "schema_version": 3,
            "recovery_complete": True,
            "user_visible_trade_proposals": {
                "proposal-1": {
                    "proposal": proposal(),
                    "status": "VISIBLE",
                    "visible_at": "2026-10-11T00:30:00Z",
                    "updated_at": "2026-10-11T00:30:00Z",
                    "timeline": [{
                        "timestamp": "2026-10-11T00:30:00Z",
                        "status": "VISIBLE",
                        "details": {},
                    }],
                }
            },
            "entry_metadata": {},
        })

        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "bot_activity.log"), "w", encoding="utf-8") as handle:
                handle.write(
                    "2026-10-11 00:30:00,000Z - TradingBot - INFO - healthy\n"
                )
            result = export_runtime_logs_zip(
                minutes=60,
                log_dir=directory,
                now=now,
                state_manager=state,
            )
            try:
                with zipfile.ZipFile(result.paths[0]) as archive:
                    names = set(archive.namelist())
                    self.assertTrue({
                        "review_summary.json",
                        "operational_snapshot.json",
                        "proposal_lifecycle.jsonl",
                        "REVIEW_GUIDE_AR.md",
                    }.issubset(names))
                    lifecycle = archive.read("proposal_lifecycle.jsonl").decode("utf-8")
                    review = json.loads(archive.read("review_summary.json"))
                self.assertIn("proposal-1", lifecycle)
                self.assertIn("current_runtime", review)
            finally:
                cleanup_export(result)


if __name__ == "__main__":
    unittest.main()
