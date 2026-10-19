import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from main import verify_recovered_position_protection
from utils.runtime_health import (
    RUN_ID,
    begin_runtime_session,
    mark_graceful_shutdown,
    resource_snapshot,
)
from utils.lazy_ai_engine import LazyAIEngine


class TestRecoveredProtection(unittest.TestCase):
    def _state(self, metadata):
        state = Mock()
        state.get_entry_metadata.side_effect = lambda symbol: dict(metadata.get(symbol, {}))
        state.save_entry_metadata.return_value = True
        return state

    def test_managed_position_must_be_verified_on_exchange(self):
        metadata = {
            "BTC/USDT": {
                "side": "BUY", "amount": 0.01, "sl": 60000, "tp": 70000,
                "trade_id": "trade-1",
            }
        }
        state = self._state(metadata)
        client = Mock()
        client.ensure_position_protection.return_value = {
            "status": "OK", "sl_ok": True, "tp_ok": True,
            "sl_order_id": "sl-1", "tp_order_id": "tp-1",
        }

        result = verify_recovered_position_protection(
            client, state, {"BTC/USDT"}
        )

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["verified"], ["BTC/USDT"])
        state.save_entry_metadata.assert_called_once()
        self.assertEqual(
            state.save_entry_metadata.call_args.args[1]["protection_status"],
            "PROTECTED",
        )

    def test_unknown_protection_blocks_recovery(self):
        state = self._state({
            "BTC/USDT": {
                "side": "BUY", "amount": 0.01, "sl": 60000, "tp": 70000,
            }
        })
        client = Mock()
        client.ensure_position_protection.return_value = {
            "status": "UNKNOWN", "sl_ok": False, "tp_ok": False,
        }

        result = verify_recovered_position_protection(
            client, state, {"BTC/USDT"}
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["errors"][0]["reason"], "PROTECTION_NOT_VERIFIED")

    def test_external_position_is_not_modified(self):
        state = self._state({
            "VELVET/USDT": {"external_position": True, "side": "BUY"}
        })
        client = Mock()

        result = verify_recovered_position_protection(
            client, state, {"VELVET/USDT"}
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["external_unmanaged"], ["VELVET/USDT"])
        client.ensure_position_protection.assert_not_called()


class TestRuntimeSession(unittest.TestCase):
    def test_ai_sdks_remain_lazy_until_an_ai_method_is_used(self):
        proxy = LazyAIEngine()
        self.assertFalse(proxy.initialized)
        self.assertNotIn("engines.ai_engine", sys.modules)

    def test_ungraceful_previous_session_is_detected_then_closed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DATA_DIR": directory}, clear=False
        ):
            path = Path(directory) / "runtime_session.json"
            path.write_text(
                json.dumps({"run_id": "old-run", "status": "RUNNING"}),
                encoding="utf-8",
            )

            result = begin_runtime_session()
            self.assertTrue(result["unexpected_restart"])
            self.assertTrue(mark_graceful_shutdown("TEST_COMPLETE"))
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["run_id"], RUN_ID)
        self.assertEqual(saved["status"], "GRACEFUL_SHUTDOWN")
        self.assertEqual(saved["exit_reason"], "TEST_COMPLETE")

    def test_new_railway_deployment_is_not_mislabeled_as_crash(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DATA_DIR": directory, "RAILWAY_DEPLOYMENT_ID": "deploy-new"},
            clear=False,
        ):
            path = Path(directory) / "runtime_session.json"
            path.write_text(
                json.dumps({
                    "run_id": "old-run",
                    "status": "RUNNING",
                    "railway_deployment_id": "deploy-old",
                }),
                encoding="utf-8",
            )

            result = begin_runtime_session()

        self.assertFalse(result["unexpected_restart"])
        self.assertTrue(result["deployment_overlap"])

    def test_old_draining_run_cannot_overwrite_new_run_pointer(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DATA_DIR": directory}, clear=False
        ), patch("utils.runtime_health.RUN_ID", "old-draining-run"):
            path = Path(directory) / "runtime_session.json"
            path.write_text(
                json.dumps({"run_id": "new-active-run", "status": "RUNNING"}),
                encoding="utf-8",
            )

            self.assertTrue(mark_graceful_shutdown("SIGNAL_15"))
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["run_id"], "new-active-run")
        self.assertEqual(saved["status"], "RUNNING")

    def test_resource_snapshot_exposes_process_values(self):
        snapshot = resource_snapshot()
        self.assertGreater(snapshot["rss_bytes"], 0)
        self.assertGreaterEqual(snapshot["peak_rss_bytes"], snapshot["rss_bytes"])
        self.assertGreater(snapshot["threads"], 0)
        self.assertEqual(snapshot["run_id"], RUN_ID)


if __name__ == "__main__":
    unittest.main()
