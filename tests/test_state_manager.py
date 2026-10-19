import os
import tempfile
import unittest
import json
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import patch
import utils.state_manager as state_module
from utils.state_manager import StateManager

class TestStateManager(unittest.TestCase):
    def setUp(self):
        # StateManager قد يكون مستورداً مسبقاً عبر main أثناء collection، لذلك
        # تغيير os.environ هنا لا يكفي. نعزل ثابت الوحدة نفسه في مجلد مؤقت.
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_state_file = os.path.join(self.temp_dir.name, "test_state.json")
        self.old_state_file = state_module.STATE_FILE
        state_module.STATE_FILE = self.test_state_file
        self.state_manager = StateManager()

    def tearDown(self):
        state_module.STATE_FILE = self.old_state_file
        self.temp_dir.cleanup()

    def test_normalize_sym(self):
        # Base check
        self.assertEqual(self.state_manager._normalize_sym("BTC/USDT:USDT"), "BTC/USDT")
        self.assertEqual(self.state_manager._normalize_sym("BTC/USDT"), "BTC/USDT")
        self.assertEqual(self.state_manager._normalize_sym("BTCUSDT"), "BTC/USDT")
        self.assertEqual(self.state_manager._normalize_sym("btc/usdt"), "BTC/USDT")
        self.assertEqual(self.state_manager._normalize_sym("btcusdt"), "BTC/USDT")

    def test_daily_risk_session_uses_utc_and_migrates_without_reset(self):
        today_utc = datetime.now(timezone.utc).date().isoformat()
        self.state_manager.state["last_reset_date"] = today_utc
        self.state_manager.state["risk_session_utc_date"] = ""
        self.state_manager.state["initial_balance"] = 123.0

        self.state_manager.update_daily_balance(999.0)

        self.assertEqual(self.state_manager.state["risk_session_utc_date"], today_utc)
        self.assertEqual(self.state_manager.state["initial_balance"], 123.0)
        self.assertEqual(self.state_manager._normalize_sym("BTC USDT"), "BTC/USDT")
        self.assertEqual(self.state_manager._normalize_sym(""), "")

    def test_active_symbols(self):
        # Save active symbols with mixed formats
        self.state_manager.save_active_symbols({"BTC/USDT:USDT", "ETH/USDT", "SOLUSDT"})
        
        # Get active symbols and assert they are normalized
        active = self.state_manager.get_active_symbols()
        self.assertEqual(active, {"BTC/USDT", "ETH/USDT", "SOL/USDT"})
        
        # Remove an active symbol using different format
        self.state_manager.remove_active_symbol("eth/usdt")
        active = self.state_manager.get_active_symbols()
        self.assertEqual(active, {"BTC/USDT", "SOL/USDT"})

    def test_trailing_trades(self):
        trades = {
            "BTC/USDT:USDT": {"best_price": 50000.0, "sl": 49000.0},
            "ETHUSDT": {"best_price": 3000.0, "sl": 2900.0}
        }
        self.state_manager.save_trailing_trades(trades)
        
        saved = self.state_manager.get_trailing_trades()
        self.assertIn("BTC/USDT", saved)
        self.assertIn("ETH/USDT", saved)
        self.assertNotIn("BTC/USDT:USDT", saved)
        self.assertNotIn("ETHUSDT", saved)

    def test_order_intent_and_runtime_settings_survive_restart(self):
        self.state_manager.save_order_intent("BTCUSDT", {
            "trade_id": "tr_restart", "status": "SUBMITTING"
        })
        self.state_manager.set_leverage(7)
        self.state_manager.set_max_open_trades(4)
        self.state_manager.save_state(force=True)

        restarted = StateManager()

        self.assertEqual(restarted.get_order_intent("BTC/USDT")["trade_id"], "tr_restart")
        self.assertEqual(restarted.get_order_intents()["BTC/USDT"]["status"], "SUBMITTING")
        self.assertEqual(restarted.get_leverage(), 7)
        self.assertEqual(restarted.get_max_open_trades(), 4)

    def test_generic_set_does_not_retain_caller_owned_mutable_state(self):
        caller_state = {"setups": {"BTCUSDT": {"status": "WAITING"}}}

        self.assertTrue(self.state_manager.set("shadow_state", caller_state))
        caller_state["setups"]["ETHUSDT"] = {"status": "NEW"}
        caller_state["setups"]["BTCUSDT"]["status"] = "CHANGED"

        stored = self.state_manager.get("shadow_state")
        self.assertEqual(
            stored,
            {"setups": {"BTCUSDT": {"status": "WAITING"}}},
        )

    def test_order_intent_bypasses_debounce_and_is_immediately_durable(self):
        self.state_manager.set("recent_write", True)
        self.assertTrue(self.state_manager.save_order_intent("ETHUSDT", {
            "trade_id": "durable-1", "status": "SUBMITTING"
        }))

        with open(self.test_state_file, "r", encoding="utf-8") as handle:
            persisted = json.load(handle)
        self.assertEqual(
            persisted["order_intents"]["ETH/USDT"]["trade_id"], "durable-1"
        )

    def test_failed_order_intent_write_rolls_back_memory(self):
        with patch.object(self.state_manager, "_save_state_unsafe", return_value=False):
            saved = self.state_manager.save_order_intent("SOLUSDT", {
                "trade_id": "must-not-remain"
            })

        self.assertFalse(saved)
        self.assertEqual(self.state_manager.get_order_intent("SOLUSDT"), {})

    def test_order_intent_transition_is_compare_and_swap_and_durable(self):
        self.assertTrue(self.state_manager.save_order_intent("BTCUSDT", {
            "trade_id": "cas-1", "status": "SUBMITTING"
        }))
        self.assertFalse(self.state_manager.transition_order_intent(
            "BTCUSDT", "PROTECTING", "PROTECTED"
        ))
        self.assertTrue(self.state_manager.transition_order_intent(
            "BTCUSDT",
            "SUBMITTING",
            "ENTRY_CONFIRMED",
            updates={"actual_amount": 0.25},
        ))

        restarted = StateManager()
        intent = restarted.get_order_intent("BTCUSDT")
        self.assertEqual(intent["status"], "ENTRY_CONFIRMED")
        self.assertEqual(intent["actual_amount"], 0.25)
        self.assertGreater(intent["updated_at"], 0)

    def test_v1_state_migrates_to_current_schema_and_is_persisted(self):
        legacy = {
            "active_symbols": ["BTC/USDT"],
            "entry_metadata": {},
            "virtual_orders": {},
            "order_intents": {},
            "trailing_trades": {},
        }
        with open(self.test_state_file, "w", encoding="utf-8") as handle:
            json.dump(legacy, handle)

        migrated = StateManager()

        self.assertEqual(migrated.get("schema_version"), 3)
        self.assertEqual(migrated.get("protection_replacements"), {})
        self.assertEqual(migrated.get("archived_order_intents"), {})
        with open(self.test_state_file, "r", encoding="utf-8") as handle:
            persisted = json.load(handle)
        self.assertEqual(persisted["schema_version"], 3)

    def test_v2_state_adds_durable_protection_saga_fields(self):
        with open(self.test_state_file, "w", encoding="utf-8") as handle:
            json.dump({
                "schema_version": 2,
                "active_symbols": [],
                "entry_metadata": {},
                "virtual_orders": {},
                "order_intents": {},
                "archived_order_intents": {},
                "trailing_trades": {},
            }, handle)

        migrated = StateManager()

        self.assertEqual(migrated.get("schema_version"), 3)
        self.assertEqual(migrated.get("protection_replacements"), {})
        self.assertEqual(migrated.get("archived_protection_replacements"), {})

    def test_future_state_schema_blocks_recovery_and_writes(self):
        future = {
            "schema_version": 999,
            "active_symbols": [],
            "entry_metadata": {},
            "virtual_orders": {},
            "order_intents": {},
            "trailing_trades": {},
        }
        with open(self.test_state_file, "w", encoding="utf-8") as handle:
            json.dump(future, handle)

        blocked = StateManager()

        self.assertTrue(blocked.is_state_recovery_blocked())
        self.assertFalse(blocked.set("must_not_overwrite", True))
        with open(self.test_state_file, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["schema_version"], 999)

    def test_future_primary_is_not_silently_replaced_by_older_backup(self):
        future = {
            "schema_version": 999,
            "active_symbols": [],
            "entry_metadata": {},
            "virtual_orders": {},
            "order_intents": {},
            "trailing_trades": {},
        }
        with open(self.test_state_file, "w", encoding="utf-8") as handle:
            json.dump(future, handle)
        with open(f"{self.test_state_file}.bak", "w", encoding="utf-8") as handle:
            json.dump({"active_symbols": ["ETH/USDT"]}, handle)

        blocked = StateManager()

        self.assertTrue(blocked.is_state_recovery_blocked())
        self.assertEqual(
            blocked.get_state_recovery_status()["source"],
            "future_schema_read_only",
        )
        with open(self.test_state_file, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["schema_version"], 999)

    def test_terminal_intent_archive_requires_positive_proof(self):
        self.assertTrue(self.state_manager.save_order_intent("ETHUSDT", {
            "trade_id": "archive-1", "status": "SUBMITTING"
        }))
        self.assertFalse(
            self.state_manager.archive_order_intent("ETHUSDT", "UNKNOWN")
        )
        self.assertTrue(self.state_manager.get_order_intent("ETHUSDT"))

        self.assertTrue(self.state_manager.archive_order_intent(
            "ETHUSDT", "ENTRY_TERMINAL_NO_FILL", {"venue_status": "REJECTED"}
        ))
        self.assertEqual(self.state_manager.get_order_intent("ETHUSDT"), {})
        archived = self.state_manager.get("archived_order_intents")
        self.assertEqual(
            archived["ETH/USDT"]["terminal_proofs"]["venue_status"],
            "REJECTED",
        )

    def test_close_intent_is_durable_and_archived_only_after_terminal_proof(self):
        intent = self.state_manager.prepare_close_intent(
            symbol="BTCUSDT",
            position_side="LONG",
            side="SELL",
            amount=0.25,
            position_id="position-1",
            reason="BOT_EXIT",
        )
        self.assertEqual(intent["status"], "PREPARED")
        restarted = StateManager()
        persisted = restarted.get_close_intent("BTC/USDT", "LONG")
        self.assertEqual(persisted["client_order_id"], intent["client_order_id"])

        self.assertFalse(restarted.archive_close_intent(
            "BTC/USDT", "LONG", "UNKNOWN"
        ))
        self.assertTrue(restarted.get_close_intent("BTC/USDT", "LONG"))
        self.assertTrue(restarted.archive_close_intent(
            "BTC/USDT", "LONG", "ZERO_CONFIRMED",
            {"position_status": "CLOSED"},
        ))
        self.assertEqual(restarted.get_close_intent("BTC/USDT", "LONG"), {})

    def test_protection_replacement_survives_restart_and_archives_with_proof(self):
        saga = self.state_manager.prepare_protection_replacement(
            symbol="BTCUSDT",
            position_side="LONG",
            side="BUY",
            amount=0.25,
            old_orders=[{"id": "old-sl", "trigger_price": 98.0}],
            old_stop=98.0,
            new_stop=99.0,
        )
        self.assertEqual(saga["phase"], "PREPARED")
        self.assertTrue(self.state_manager.transition_protection_replacement(
            "BTCUSDT", "LONG", "PREPARED", "CANCEL_IN_PROGRESS"
        ))

        recovered = StateManager()
        pending = recovered.get_protection_replacement("BTCUSDT", "LONG")
        self.assertEqual(pending["phase"], "CANCEL_IN_PROGRESS")
        self.assertTrue(recovered.archive_protection_replacement(
            "BTCUSDT", "LONG", "NEW_VERIFIED",
            proofs={"verified_stop": 99.0},
        ))
        self.assertEqual(
            recovered.get_protection_replacement("BTCUSDT", "LONG"), {}
        )
        archived = recovered.get("archived_protection_replacements")
        self.assertEqual(
            archived["BTC/USDT|LONG#1"]["terminal_proofs"]["verified_stop"],
            99.0,
        )

    def test_runtime_risk_values_cannot_exceed_hard_policy(self):
        self.assertFalse(self.state_manager.set_risk_pct(2.0))
        self.assertFalse(self.state_manager.set_leverage(50))
        self.assertFalse(self.state_manager.set_max_open_trades(11))
        self.assertIsNone(self.state_manager.get("custom_risk_pct"))
        self.assertIsNone(self.state_manager.get("custom_leverage"))
        self.assertIsNone(self.state_manager.get("max_open_trades"))

        self.assertTrue(self.state_manager.set_risk_pct(0.25))
        self.assertTrue(self.state_manager.set_leverage(5))
        self.assertTrue(self.state_manager.set_max_open_trades(10))

    def test_virtual_order_creation_and_removal_are_durable(self):
        self.assertTrue(self.state_manager.save_virtual_order("XRPUSDT", {
            "trade_id": "virtual-durable", "status": "PENDING"
        }))
        restarted = StateManager()
        self.assertIn("XRP/USDT", restarted.get_virtual_orders())

        self.assertTrue(self.state_manager.remove_virtual_order("XRPUSDT"))
        restarted_again = StateManager()
        self.assertNotIn("XRP/USDT", restarted_again.get_virtual_orders())

    def test_virtual_order_with_timestamp_is_json_safe_and_durable(self):
        evidence_time = pd.Timestamp("2026-08-23T00:00:00Z")
        self.assertTrue(self.state_manager.save_virtual_order("NEIROUSDT", {
            "trade_id": "timestamp-safe",
            "invalidation_state": "RECLAIM_CONFIRMED",
            "structural_close_evidence": {"open_time": evidence_time},
        }))

        restarted = StateManager()
        saved = restarted.get_virtual_orders()["NEIRO/USDT"]
        self.assertEqual(
            saved["structural_close_evidence"]["open_time"],
            evidence_time.isoformat(),
        )

    def test_setup_cooldown_is_durable_and_scoped_to_thesis(self):
        self.assertTrue(self.state_manager.set_setup_cooldown(
            "ETHUSDT", "setup_old", 60, "STOP_LOSS"
        ))
        restarted = StateManager()

        self.assertEqual(
            restarted.get_setup_cooldown("ETH/USDT", "setup_old")["reason"],
            "STOP_LOSS",
        )
        self.assertEqual(
            restarted.get_setup_cooldown("ETH/USDT", "setup_new"), {}
        )

    def test_expired_setup_cooldown_is_removed(self):
        self.assertTrue(self.state_manager.set_setup_cooldown(
            "SOLUSDT", "setup_expired", 60, "TIMEOUT"
        ))
        self.state_manager.state["setup_cooldowns"]["SOL/USDT"]["setup_expired"]["expires_at"] = 1
        self.state_manager.save_state(force=True)

        self.assertEqual(
            self.state_manager.get_setup_cooldown("SOLUSDT", "setup_expired"), {}
        )

    def test_entry_metadata(self):
        meta = {"entry_price": 50000.0, "sl": 49000.0, "tp": 55000.0}
        self.state_manager.save_entry_metadata("BTC/USDT:USDT", meta)
        
        # Retrieval checks
        self.assertEqual(self.state_manager.get_entry_metadata("BTC/USDT")["entry_price"], 50000.0)
        self.assertEqual(self.state_manager.get_entry_metadata("btc/usdt")["entry_price"], 50000.0)
        self.assertEqual(self.state_manager.get_entry_metadata("BTC/USDT:USDT")["entry_price"], 50000.0)
        
        # Pop check
        popped = self.state_manager.pop_entry_metadata("BTCUSDT")
        self.assertEqual(popped["entry_price"], 50000.0)
        self.assertEqual(self.state_manager.get_entry_metadata("BTC/USDT"), {})

    def test_freeze_pairs(self):
        self.state_manager.freeze_pair("BTC/USDT:USDT", 2)
        self.assertTrue(self.state_manager.is_pair_frozen("BTC/USDT"))
        self.assertTrue(self.state_manager.is_pair_frozen("BTCUSDT"))
        
        frozen = self.state_manager.get_frozen_pairs()
        self.assertIn("BTC/USDT", frozen)
        self.assertNotIn("BTC/USDT:USDT", frozen)

if __name__ == "__main__":
    unittest.main()
