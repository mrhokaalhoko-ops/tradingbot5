import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from core.exchange import ExchangeClient
from main import restore_recovered_trailing_tracking
from strategy.trailing_stop import TrailingStopManager
from utils.single_writer import SingleWriterLease
import utils.state_manager as state_module
from utils.state_manager import StateManager
from utils.websocket_manager import FuturesWebSocketManager


class TestExecutionFailsClosed(unittest.TestCase):
    def _client(self):
        client = ExchangeClient.__new__(ExchangeClient)
        client.exchange = Mock()
        client._is_hedge_mode = False
        client.exchange.markets = {
            "BTC/USDT": {
                "symbol": "BTC/USDT",
                "type": "swap",
                "contract": True,
                "linear": True,
                "quote": "USDT",
                "settle": "USDT",
                "active": True,
                "limits": {
                    "amount": {"min": 0.001, "max": 100.0},
                    "cost": {"min": 5.0, "max": None},
                },
            }
        }
        client.exchange.amount_to_precision.side_effect = lambda _symbol, amount: str(amount)
        client.exchange.set_leverage.return_value = {"leverage": 5}
        client.exchange.create_order.return_value = {"id": "entry-1", "filled": 0.1}
        return client

    def test_leverage_exception_blocks_entry_order(self):
        client = self._client()
        client.exchange.set_leverage.side_effect = RuntimeError("timeout")

        result = client.execute_trade(
            "BTC/USDT", "buy", 0.1, current_price=100.0, leverage=5
        )

        self.assertIsNone(result)
        client.exchange.create_order.assert_not_called()

    def test_unconfirmed_leverage_response_blocks_entry_order(self):
        client = self._client()
        client.exchange.set_leverage.return_value = {"symbol": "BTCUSDT"}

        result = client.execute_trade(
            "BTC/USDT", "buy", 0.1, current_price=100.0, leverage=5
        )

        self.assertIsNone(result)
        client.exchange.create_order.assert_not_called()

    def test_missing_market_limits_block_entry_order(self):
        client = self._client()
        client.exchange.markets["BTC/USDT"]["limits"] = {}

        result = client.execute_trade(
            "BTC/USDT", "buy", 0.1, current_price=100.0, leverage=5
        )

        self.assertIsNone(result)
        client.exchange.create_order.assert_not_called()

    def test_futures_only_symbol_resolves_colon_contract_metadata(self):
        client = self._client()
        market = client.exchange.markets.pop("BTC/USDT")
        market["symbol"] = "FHE/USDT:USDT"
        market["limits"]["amount"]["max"] = 100000.0
        client.exchange.markets["FHE/USDT:USDT"] = market

        result = client.execute_trade(
            "FHE/USDT", "buy", 1000.0, current_price=0.02129, leverage=5
        )

        self.assertIsNotNone(result)
        client.exchange.set_leverage.assert_called_once_with(5, "FHE/USDT:USDT")
        self.assertEqual(
            client.exchange.create_order.call_args.args[0], "FHE/USDT:USDT"
        )

    def test_spot_metadata_is_never_used_for_futures_entry(self):
        client = self._client()
        contract = client.exchange.markets.pop("BTC/USDT")
        contract["symbol"] = "FHE/USDT:USDT"
        contract["limits"]["amount"]["max"] = 100000.0
        client.exchange.markets = {
            "FHE/USDT": {
                "symbol": "FHE/USDT",
                "type": "spot",
                "spot": True,
                "active": True,
                "limits": {
                    "amount": {"min": 999999.0},
                    "cost": {"min": 999999.0},
                },
            },
            "FHE/USDT:USDT": contract,
        }

        result = client.execute_trade(
            "FHE/USDT", "buy", 1000.0, current_price=0.02129, leverage=5
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            client.exchange.amount_to_precision.call_args.args[0],
            "FHE/USDT:USDT",
        )
        self.assertEqual(
            client.exchange.create_order.call_args.args[0], "FHE/USDT:USDT"
        )

    def test_spot_only_symbol_fails_before_any_order_submission(self):
        client = self._client()
        client.exchange.markets = {
            "FHE/USDT": {
                "symbol": "FHE/USDT",
                "type": "spot",
                "spot": True,
                "active": True,
            }
        }

        result = client.execute_trade(
            "FHE/USDT", "buy", 10.0, current_price=0.02129, leverage=5
        )

        self.assertIsNone(result)
        self.assertEqual(
            client.last_execution_failure["code"],
            "USDM_CONTRACT_METADATA_UNAVAILABLE",
        )
        self.assertFalse(client.last_execution_failure["send_attempted"])
        client.exchange.set_leverage.assert_not_called()
        client.exchange.create_order.assert_not_called()


class TestPriceFreshnessPolicy(unittest.TestCase):
    def test_stale_cache_is_not_returned_when_rest_fails(self):
        manager = FuturesWebSocketManager()
        manager._prices["BTCUSDT"] = 100.0
        manager._timestamps["BTCUSDT"] = time.time() - 3600

        price = manager.get_price_safe("BTC/USDT", fallback_fn=lambda _symbol: None)

        self.assertEqual(price, 0.0)
        self.assertEqual(manager.get_price_snapshot("BTC/USDT")["status"], "STALE")

    def test_stale_cache_may_be_requested_for_display_only(self):
        manager = FuturesWebSocketManager()
        manager._prices["BTCUSDT"] = 100.0
        manager._timestamps["BTCUSDT"] = time.time() - 3600

        self.assertEqual(
            manager.get_price_safe(
                "BTC/USDT", fallback_fn=lambda _symbol: None, allow_stale=True
            ),
            100.0,
        )


class TestStateCorruptionRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp_dir.name, "bot_state.json")
        self.old_path = state_module.STATE_FILE
        state_module.STATE_FILE = self.path

    def tearDown(self):
        state_module.STATE_FILE = self.old_path
        self.temp_dir.cleanup()

    def test_corrupt_primary_recovers_last_valid_backup(self):
        manager = StateManager()
        manager.state["kill_switch_active"] = True
        self.assertTrue(manager.save_state(force=True))
        manager.state["max_open_trades"] = 7
        self.assertTrue(manager.save_state(force=True))
        self.assertTrue(os.path.exists(self.path + ".bak"))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{broken")

        recovered = StateManager()

        self.assertFalse(recovered.is_state_recovery_blocked())
        self.assertTrue(recovered.get("kill_switch_active"))
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertIsInstance(json.load(handle), dict)

    def test_corrupt_primary_without_backup_blocks_writes(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        manager = StateManager()

        self.assertTrue(manager.is_state_recovery_blocked())
        self.assertFalse(manager.set("kill_switch_active", False))
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{broken")


class TestSingleWriterAndTrailingRecovery(unittest.TestCase):
    def test_second_writer_cannot_acquire_same_volume_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "writer.lock")
            first = SingleWriterLease(path=path, owner_id="first")
            second = SingleWriterLease(path=path, owner_id="second")
            try:
                self.assertTrue(first.acquire(timeout_seconds=0))
                self.assertFalse(second.acquire(timeout_seconds=0))
                first.release()
                self.assertTrue(second.acquire(timeout_seconds=0))
            finally:
                first.release()
                second.release()

    def test_restart_rebuilds_missing_trailing_state_once(self):
        state = Mock()
        state.get_entry_metadata.return_value = {
            "side": "BUY",
            "entry_price": 100.0,
            "initial_sl": 95.0,
            "current_sl": 101.0,
            "amount": 2.0,
        }
        state.save_trailing_trades.return_value = True
        manager = TrailingStopManager()
        manager._state_manager = state
        client = Mock()
        client.get_current_price.return_value = 105.0

        first = restore_recovered_trailing_tracking(
            client, state, manager, {"BTC/USDT"}
        )
        second = restore_recovered_trailing_tracking(
            client, state, manager, {"BTC/USDT"}
        )

        self.assertEqual(first["status"], "OK")
        self.assertEqual(first["restored"], ["BTC/USDT"])
        self.assertEqual(second["restored"], [])
        trade = manager.get_trade_info("BTC/USDT")
        self.assertEqual(trade["initial_sl"], 95.0)
        self.assertEqual(trade["current_sl"], 101.0)
        self.assertEqual(trade["best_price"], 105.0)


if __name__ == "__main__":
    unittest.main()
