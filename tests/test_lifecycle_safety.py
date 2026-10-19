import unittest
from unittest.mock import Mock

from core.exchange import ExchangeClient
from main import (
    classify_scan_result_event,
    finalize_virtual_order,
    reconcile_positions,
    run_bot_iteration,
)


class _StateStub:
    def __init__(self, active=None):
        self.active = set(active or [])
        self.metadata = {}
        self.saved_active = None

    def get_active_symbols(self):
        return set(self.active)

    def save_active_symbols(self, symbols):
        self.active = set(symbols)
        self.saved_active = set(symbols)
        return True

    def get_entry_metadata(self, symbol):
        return dict(self.metadata.get(symbol, {}))

    def save_entry_metadata(self, symbol, data):
        self.metadata[symbol] = dict(data)
        return True


class TestPositionStatusSafety(unittest.TestCase):
    def _client(self):
        client = ExchangeClient.__new__(ExchangeClient)
        client.exchange = Mock()
        return client

    def test_api_failure_is_unknown_not_closed(self):
        client = self._client()
        client.exchange.fetch_positions.side_effect = RuntimeError("network down")

        status = client.get_position_status("BTC/USDT")

        self.assertEqual(status["status"], "UNKNOWN")
        self.assertIsNone(client.has_open_position("BTC/USDT"))
        self.assertIsNone(client.get_position_amount("BTC/USDT"))

    def test_open_position_returns_real_amount(self):
        client = self._client()
        client.exchange.fetch_positions.return_value = [{
            "symbol": "BTC/USDT:USDT",
            "entryPrice": 100.0,
            "info": {"positionAmt": "0.25", "positionSide": "LONG"},
        }]

        status = client.get_position_status("BTC/USDT")

        self.assertEqual(status["status"], "OPEN")
        self.assertAlmostEqual(status["amount"], 0.25)
        self.assertTrue(client.has_open_position("BTC/USDT"))


class TestReconciliationSafety(unittest.TestCase):
    def test_stale_symbol_is_kept_until_close_is_recorded(self):
        client = Mock()
        client.exchange.fetch_positions.return_value = []
        state = _StateStub(active={"LAB/USDT"})

        result = reconcile_positions(client, state)

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["pending_close"], {"LAB/USDT"})
        self.assertEqual(state.active, {"LAB/USDT"})
        self.assertIsNone(state.saved_active)

    def test_external_position_gets_minimum_metadata(self):
        client = Mock()
        client.exchange.fetch_positions.return_value = [{
            "symbol": "ETH/USDT:USDT",
            "entryPrice": 2500.0,
            "info": {
                "positionAmt": "-0.2",
                "positionSide": "SHORT",
                "entryPrice": "2500.0",
            },
        }]
        state = _StateStub()

        result = reconcile_positions(client, state)

        self.assertEqual(result["status"], "OK")
        self.assertEqual(state.active, {"ETH/USDT"})
        self.assertEqual(state.metadata["ETH/USDT"]["side"], "SELL")
        self.assertEqual(state.metadata["ETH/USDT"]["amount"], 0.2)
        self.assertTrue(state.metadata["ETH/USDT"]["external_position"])

    def test_reconcile_failure_does_not_mutate_state(self):
        client = Mock()
        client.exchange.fetch_positions.side_effect = RuntimeError("timeout")
        state = _StateStub(active={"BTC/USDT"})

        result = reconcile_positions(client, state)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(state.active, {"BTC/USDT"})
        self.assertIsNone(state.saved_active)

    def test_live_active_symbol_without_metadata_is_recovered_after_restart(self):
        client = Mock()
        client.exchange.fetch_positions.return_value = [{
            "symbol": "BTC/USDT:USDT",
            "entryPrice": 50000.0,
            "info": {
                "positionAmt": "0.01",
                "positionSide": "BOTH",
                "entryPrice": "50000.0",
            },
        }]
        state = _StateStub(active={"BTC/USDT"})

        result = reconcile_positions(client, state)

        self.assertEqual(result["metadata_recovered"], {"BTC/USDT"})
        self.assertEqual(state.metadata["BTC/USDT"]["position_side"], "LONG")
        self.assertEqual(state.metadata["BTC/USDT"]["amount"], 0.01)

    def test_known_bot_long_plus_external_short_blocks_management(self):
        client = Mock()
        client.exchange.fetch_positions.return_value = [
            {
                "symbol": "BTC/USDT:USDT",
                "entryPrice": 50000.0,
                "info": {
                    "positionAmt": "0.01", "positionSide": "LONG",
                    "entryPrice": "50000.0",
                },
            },
            {
                "symbol": "BTC/USDT:USDT",
                "entryPrice": 50500.0,
                "info": {
                    "positionAmt": "-0.02", "positionSide": "SHORT",
                    "entryPrice": "50500.0",
                },
            },
        ]
        state = _StateStub(active={"BTC/USDT"})
        state.metadata["BTC/USDT"] = {
            "side": "BUY", "position_side": "LONG",
            "entry_price": 50000.0, "amount": 0.01,
            "entry_reason": "BOT_ENTRY", "strategy_source": "SMC",
        }

        result = reconcile_positions(client, state)

        self.assertEqual(result["management_blocked"], {"BTC/USDT"})
        meta = state.metadata["BTC/USDT"]
        self.assertTrue(meta["management_blocked"])
        self.assertEqual(meta["management_blocked_reason"], "EXTERNAL_HEDGE_CONFLICT")
        self.assertEqual(meta["observed_position_sides"], ["LONG", "SHORT"])
        self.assertEqual(meta["side"], "BUY")

    def test_new_external_position_sends_explicit_unmanaged_alert(self):
        notifier = Mock()
        with unittest.mock.patch(
            "main.reconcile_positions",
            return_value={"status": "OK", "missing": {"VELVET/USDT"}},
        ), unittest.mock.patch(
            "main.enforce_daily_loss_limit",
            return_value={"status": "UNKNOWN"},
        ), unittest.mock.patch("main.audit_decision_event") as audit:
            run_bot_iteration(
                Mock(), Mock(), Mock(), Mock(), Mock(), notifier,
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), [],
            )

        notifier.send_message.assert_called_once()
        message = notifier.send_message.call_args.args[0]
        self.assertIn("VELVET/USDT", message)
        self.assertIn("مركز خارجي أو غير متتبّع", message)
        self.assertIn("إيقاف فتح صفقات جديدة", message)
        statuses = [call.kwargs.get("status") for call in audit.call_args_list]
        self.assertIn("UNTRACKED_POSITION_DISCOVERED_TRADING_PAUSED", statuses)
        self.assertEqual(statuses[0], "STARTED")
        self.assertEqual(statuses[-1], "ABORTED_SAFE")


class TestVirtualOrderFinalization(unittest.TestCase):
    def test_timeout_updates_forensics_before_removing_order(self):
        events = []
        state = Mock()
        state.remove_virtual_order.side_effect = lambda symbol: events.append(("remove", symbol))
        notifier = Mock()

        def record_outcome(trade_id, outcome, pnl):
            events.append(("outcome", trade_id, outcome, pnl))

        with unittest.mock.patch(
            "utils.forensics_logger.update_signal_outcome", side_effect=record_outcome
        ):
            finalize_virtual_order(
                state,
                notifier,
                "BTC/USDT",
                {"trade_id": "virtual-1"},
                "EXPIRED_VIRTUAL_TIMEOUT",
            )

        self.assertEqual(events[0][0], "outcome")
        self.assertEqual(events[0][2], "EXPIRED_VIRTUAL_TIMEOUT")
        self.assertEqual(events[1], ("remove", "BTC/USDT"))


class TestDecisionAuditClassification(unittest.TestCase):
    def test_active_external_position_is_a_scan_skip_not_terminal_trade(self):
        self.assertEqual(
            classify_scan_result_event("⏭️ ACTIVE"),
            ("SCAN_SKIP", "ACTIVE_POSITION"),
        )

    def test_existing_pending_order_is_a_scan_skip(self):
        self.assertEqual(
            classify_scan_result_event("⏳ PENDING"),
            ("SCAN_SKIP", "PENDING_ORDER"),
        )

    def test_new_virtual_limit_is_a_terminal_pending_result(self):
        self.assertEqual(
            classify_scan_result_event("⏳ V-LIMIT"),
            ("FINAL_OUTCOME", "PENDING"),
        )


if __name__ == "__main__":
    unittest.main()
