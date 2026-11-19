import os
import tempfile
import time
import unittest
from unittest.mock import Mock

from core.exchange import ExchangeClient
import utils.state_manager as state_module
from utils.state_manager import StateManager


class TestProtectionFailureInjection(unittest.TestCase):
    """Fake-exchange coverage for the durable stop replacement contract."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_state_file = state_module.STATE_FILE
        state_module.STATE_FILE = os.path.join(self.temp_dir.name, "state.json")
        self.state = StateManager()
        self.state.save_entry_metadata("BTC/USDT", {
            "trade_id": "trade-1",
            "side": "BUY",
            "sl": 98.0,
            "initial_sl": 98.0,
            "current_sl": 98.0,
        })

    def tearDown(self):
        state_module.STATE_FILE = self.previous_state_file
        self.temp_dir.cleanup()

    @staticmethod
    def _stop(order_id, price, client_id=None):
        return {
            "id": order_id,
            "clientOrderId": client_id or f"client-{order_id}",
            "status": "NEW",
            "type": "stop_market",
            "stopPrice": float(price),
            "info": {
                "orderType": "STOP_MARKET",
                "triggerPrice": str(price),
                "clientAlgoId": client_id or f"client-{order_id}",
                "algoId": order_id,
                "_algo_order": True,
            },
        }

    def _client(self, state=None):
        client = ExchangeClient.__new__(ExchangeClient)
        client.exchange = Mock()
        client.exchange.market.return_value = {"id": "BTCUSDT"}
        client.exchange.price_to_precision.side_effect = lambda _symbol, value: str(value)
        client._is_hedge_mode = False
        client.bind_state_manager(state or self.state)
        client.get_position_status = Mock(return_value={
            "status": "OPEN", "amount": 0.1, "positions": [],
        })
        client.close_position = Mock(return_value=True)
        client.last_protection_replacement_failure = None
        return client

    def _prepare(self, old_stop=98.0, new_stop=99.0):
        saga = self.state.prepare_protection_replacement(
            symbol="BTC/USDT",
            position_side="LONG",
            side="BUY",
            amount=0.1,
            old_orders=[{
                "id": "old-stop", "client_order_id": "old-client",
                "trigger_price": old_stop,
            }] if old_stop else [],
            old_stop=old_stop,
            new_stop=new_stop,
        )
        self.assertTrue(saga)
        return saga

    def _set_phase(self, phase, **updates):
        self.assertTrue(self.state.transition_protection_replacement(
            "BTC/USDT", "LONG", None, phase,
            updates={"retry_after": 0.0, **updates},
        ))
        return self.state.get_protection_replacement("BTC/USDT", "LONG")

    def test_timeout_after_submission_never_blind_retries_post(self):
        saga = self._prepare()
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client._submit_or_resolve_protection = Mock(return_value={
            "status": "UNKNOWN", "reason": "TimeoutError", "error": "timeout",
        })

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))
        self.assertEqual(client._submit_or_resolve_protection.call_count, 1)
        pending = self._set_phase("NEW_STATUS_UNKNOWN")
        client.resolve_algo_order_by_client_id = Mock(return_value={
            "status": "UNKNOWN", "order": None, "reason": "TimeoutError",
        })

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=pending))
        self.assertEqual(client._submit_or_resolve_protection.call_count, 1)
        self.assertEqual(
            self.state.get_protection_replacement("BTC/USDT", "LONG")["phase"],
            "PROTECTION_UNCERTAIN",
        )

    def test_exchange_accepted_local_unknown_recovers_by_query_without_post(self):
        saga = self._prepare()
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client._submit_or_resolve_protection = Mock(return_value={
            "status": "UNKNOWN", "reason": "TimeoutError", "error": "timeout",
        })
        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))
        self.assertEqual(client._submit_or_resolve_protection.call_count, 1)

        pending = self._set_phase("NEW_STATUS_UNKNOWN")
        client.resolve_algo_order_by_client_id = Mock(return_value={
            "status": "FOUND", "order": self._stop(
                "new-stop", 99.0, pending["new_client_order_id"]
            ),
        })
        self.assertEqual(
            client.recover_sl_replacement("BTC/USDT", saga=pending), 99.0
        )
        self.assertEqual(client._submit_or_resolve_protection.call_count, 1)
        self.assertEqual(
            self.state.get_entry_metadata("BTC/USDT")["current_sl"], 99.0
        )
        self.assertEqual(
            self.state.get_protection_replacement("BTC/USDT", "LONG"), {}
        )

    def test_restart_during_pending_reuses_persisted_intent_idempotently(self):
        saga = self._prepare()
        client1 = self._client()
        client1._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client1._submit_or_resolve_protection = Mock(return_value={
            "status": "ACKNOWLEDGED", "order": {"id": "new-stop"},
        })
        self.assertFalse(client1.recover_sl_replacement("BTC/USDT", saga=saga))
        self.assertEqual(client1._submit_or_resolve_protection.call_count, 1)

        persisted = self._set_phase("NEW_ACKNOWLEDGED")
        restarted_state = StateManager()
        client2 = self._client(restarted_state)
        client2._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client2._submit_or_resolve_protection = Mock()
        client2.resolve_algo_order_by_client_id = Mock(return_value={
            "status": "FOUND", "order": self._stop(
                "new-stop", 99.0, persisted["new_client_order_id"]
            ),
        })

        self.assertEqual(
            client2.recover_sl_replacement("BTC/USDT", saga=persisted), 99.0
        )
        client2._submit_or_resolve_protection.assert_not_called()
        self.assertEqual(
            restarted_state.get_protection_replacement("BTC/USDT", "LONG"), {}
        )

    def test_old_valid_stop_remains_when_upgrade_status_is_uncertain(self):
        self._prepare()
        pending = self._set_phase("NEW_STATUS_UNKNOWN")
        old = self._stop("old-stop", 98.0)
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [old], "tp_orders": [],
        })
        client.resolve_algo_order_by_client_id = Mock(return_value={
            "status": "UNKNOWN", "order": None, "reason": "ConnectionReset",
        })
        client._cancel_protection_order = Mock()
        client._submit_or_resolve_protection = Mock()

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=pending))
        client._cancel_protection_order.assert_not_called()
        client._submit_or_resolve_protection.assert_not_called()
        self.assertEqual(old["stopPrice"], 98.0)

    def test_snapshot_uncertainty_never_cancels_or_replaces_old_stop(self):
        saga = self._prepare()
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "UNKNOWN", "sl_orders": [], "tp_orders": [],
        })
        client._cancel_protection_order = Mock()
        client._submit_or_resolve_protection = Mock()

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))
        client._cancel_protection_order.assert_not_called()
        client._submit_or_resolve_protection.assert_not_called()
        self.assertEqual(
            self.state.get_protection_replacement("BTC/USDT", "LONG")["phase"],
            "WAIT_SNAPSHOT",
        )

    def test_authoritative_not_found_retains_old_stop_as_safe_noop(self):
        self._prepare()
        pending = self._set_phase("NEW_STATUS_UNKNOWN")
        old = self._stop("old-stop", 98.0)
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [old], "tp_orders": [],
        })
        client.resolve_algo_order_by_client_id = Mock(return_value={
            "status": "NOT_FOUND", "order": None,
            "reason": "BINANCE_ALGO_ORDER_DOES_NOT_EXIST",
        })
        client._cancel_protection_order = Mock()
        client._submit_or_resolve_protection = Mock()

        self.assertEqual(
            client.recover_sl_replacement("BTC/USDT", saga=pending), 98.0
        )
        client._cancel_protection_order.assert_not_called()
        client._submit_or_resolve_protection.assert_not_called()
        self.assertEqual(
            self.state.get_protection_replacement("BTC/USDT", "LONG"), {}
        )

    def test_same_or_more_protective_stop_is_noop_without_cancel_or_post(self):
        old = self._stop("old-stop", 99.0)
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [old], "tp_orders": [],
        })
        client._cancel_protection_order = Mock()
        client._submit_or_resolve_protection = Mock()

        result = client.replace_sl_order_state("BTC/USDT", "BUY", 0.1, 98.5)

        self.assertEqual(result["state"], "NOOP_PROTECTED")
        self.assertEqual(result["confirmed_stop"], 99.0)
        client._cancel_protection_order.assert_not_called()
        client._submit_or_resolve_protection.assert_not_called()

    def test_duplicate_replacement_event_submits_new_stop_exactly_once(self):
        old = self._stop("old-stop", 98.0)
        client = self._client()
        client._fetch_position_protection_orders = Mock(side_effect=[
            {"status": "OK", "sl_orders": [old], "tp_orders": []},
            {"status": "OK", "sl_orders": [old], "tp_orders": []},
            {"status": "OK", "sl_orders": [], "tp_orders": []},
        ])
        client._cancel_protection_order = Mock(return_value=True)
        client._submit_or_resolve_protection = Mock(return_value={
            "status": "ACKNOWLEDGED", "order": {"id": "new-stop"},
        })

        first = client.replace_sl_order_state("BTC/USDT", "BUY", 0.1, 99.0)
        second = client.replace_sl_order_state("BTC/USDT", "BUY", 0.1, 99.0)

        self.assertEqual(first["state"], "PENDING_VERIFICATION")
        self.assertEqual(second["state"], "PENDING_VERIFICATION")
        self.assertEqual(client._submit_or_resolve_protection.call_count, 1)
        self.assertEqual(client._cancel_protection_order.call_count, 1)

    def test_new_stop_query_is_authoritative_for_local_recovery(self):
        self._prepare()
        pending = self._set_phase("NEW_ACKNOWLEDGED")
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client.resolve_algo_order_by_client_id = Mock(return_value={
            "status": "FOUND", "order": self._stop(
                "exchange-stop", 99.25, pending["new_client_order_id"]
            ),
        })
        client._submit_or_resolve_protection = Mock()

        self.assertEqual(
            client.recover_sl_replacement("BTC/USDT", saga=pending), 99.25
        )
        client._submit_or_resolve_protection.assert_not_called()
        meta = self.state.get_entry_metadata("BTC/USDT")
        self.assertEqual(meta["current_sl"], 99.25)
        self.assertEqual(meta["sl_order_id"], "exchange-stop")

    def test_exchange_confirmed_stop_recovers_after_local_persist_failure(self):
        saga = self._prepare()
        confirmed = self._stop("exchange-stop", 99.0, saga["new_client_order_id"])
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [confirmed], "tp_orders": [],
        })
        original_save = self.state.save_entry_metadata
        attempts = {"count": 0}

        def fail_once(symbol, metadata):
            attempts["count"] += 1
            if attempts["count"] == 1:
                return False
            return original_save(symbol, metadata)

        self.state.save_entry_metadata = Mock(side_effect=fail_once)

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))
        pending = self.state.get_protection_replacement("BTC/USDT", "LONG")
        self.assertEqual(pending["phase"], "NEW_VERIFIED_STATE_PENDING")
        pending = self._set_phase("NEW_VERIFIED_STATE_PENDING")

        self.assertEqual(
            client.recover_sl_replacement("BTC/USDT", saga=pending), 99.0
        )
        self.assertEqual(
            self.state.get_entry_metadata("BTC/USDT")["current_sl"], 99.0
        )
        self.assertEqual(
            self.state.get_protection_replacement("BTC/USDT", "LONG"), {}
        )

    def test_restore_failure_requests_emergency_close_once(self):
        saga = self._prepare()
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client._submit_or_resolve_protection = Mock(side_effect=[
            {"status": "FAILED_DEFINITIVE", "reason": "REJECTED"},
            {"status": "FAILED_DEFINITIVE", "reason": "REJECTED"},
        ])

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))
        client.close_position.assert_called_once_with("BTC/USDT", "buy", 0.1)
        emergency = self._set_phase("EMERGENCY_CLOSE_REQUESTED")
        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=emergency))
        self.assertEqual(client.close_position.call_count, 1)

    def test_restore_pending_is_queried_not_resubmitted_after_restart(self):
        self._prepare()
        pending = self._set_phase("RESTORE_STATUS_UNKNOWN")
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client.resolve_algo_order_by_client_id = Mock(return_value={
            "status": "FOUND", "order": self._stop(
                "restored-old", 98.0, pending["restore_client_order_id"]
            ),
        })
        client._submit_or_resolve_protection = Mock()

        self.assertEqual(
            client.recover_sl_replacement("BTC/USDT", saga=pending), 98.0
        )
        client._submit_or_resolve_protection.assert_not_called()
        self.assertEqual(
            self.state.get_protection_replacement("BTC/USDT", "LONG"), {}
        )

    def test_multiple_stop_detection_blocks_all_mutations(self):
        saga = self._prepare()
        client = self._client()
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK",
            "sl_orders": [self._stop("one", 98.0), self._stop("two", 97.0)],
            "tp_orders": [],
        })
        client._cancel_protection_order = Mock()
        client._submit_or_resolve_protection = Mock()

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))
        client._cancel_protection_order.assert_not_called()
        client._submit_or_resolve_protection.assert_not_called()
        self.assertEqual(
            self.state.get_protection_replacement("BTC/USDT", "LONG")["phase"],
            "BLOCKED_MULTIPLE_STOPS",
        )

    def test_algo_query_distinguishes_found_not_found_and_unknown(self):
        client = self._client()
        client.exchange.fapiPrivateGetAlgoOrder.return_value = {
            "algoId": "algo-1", "clientAlgoId": "client-1",
            "algoStatus": "NEW", "orderType": "STOP_MARKET",
            "triggerPrice": "99.0", "side": "SELL",
        }
        found = client.resolve_algo_order_by_client_id("BTC/USDT", "client-1")
        self.assertEqual(found["status"], "FOUND")
        self.assertEqual(found["order"]["stopPrice"], 99.0)

        not_found_error = RuntimeError("Binance -2013 order does not exist")
        client.exchange.fapiPrivateGetAlgoOrder.side_effect = not_found_error
        not_found = client.resolve_algo_order_by_client_id("BTC/USDT", "missing")
        self.assertEqual(not_found["status"], "NOT_FOUND")

        client.exchange.fapiPrivateGetAlgoOrder.side_effect = TimeoutError("timeout")
        unknown = client.resolve_algo_order_by_client_id("BTC/USDT", "uncertain")
        self.assertEqual(unknown["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
