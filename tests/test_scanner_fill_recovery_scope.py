"""Scanner fill recovery follows tracked positions, independently of market rank."""
import unittest
from unittest.mock import Mock, patch

from main import run_bot_iteration


class TestScannerFillRecoveryScope(unittest.TestCase):
    def run_scan(self, active=(), *, alerts=(), position_amount=0, risk_status="OK"):
        client, state, notifier, trailing = Mock(), Mock(), Mock(), Mock()
        state.get_active_symbols.return_value = set(active)
        state.get.side_effect = lambda key, default=None: default
        state.get_entry_metadata.return_value = {"side": "BUY", "trade_id": "owned"}
        client.exchange.fetch_positions.return_value = []
        client.get_top_volatile_symbols.return_value = ["UNRELATED/USDT:USDT"]
        client.get_recent_sl_tp_fills.side_effect = lambda symbol, *a, **kw: (
            list(alerts) if symbol == "QUIET/USDT" else []
        )
        client.get_position_amount.return_value = position_amount
        trailing.has_trade.return_value = False
        risk = {"status": risk_status, "equity": 100, "available": 0}
        # Stop after fill recovery at NO_AVAILABLE_BALANCE, before discovery.
        with patch("main.audit_decision_event"), patch("main.log_memory_milestone"), \
             patch("main.verify_recovered_position_protection", return_value={"status": "OK"}), \
             patch("main.enforce_daily_loss_limit", return_value=risk), \
             patch("main.normalize_reference_balance_in_state"), \
             patch("main.handle_trade_close", return_value={"pnl": 1, "pnl_pct": 1}) as close:
            state.attach_mock(close, "record_close")
            run_bot_iteration(
                client, Mock(), Mock(), trailing, state, notifier,
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), [],
                last_scan_time=1788832800000,
            )
        return client, state, notifier, close

    def test_empty_account_does_not_request_market_ranking_or_fill_history(self):
        client, state, _, close = self.run_scan()
        client.exchange.fetch_positions.assert_called_once()
        client.get_top_volatile_symbols.assert_not_called()
        client.get_recent_sl_tp_fills.assert_not_called()
        state.remove_active_symbol.assert_not_called()
        close.assert_not_called()

    def test_tracked_symbol_outside_market_ranking_is_still_checked(self):
        client, _, _, _ = self.run_scan(active={"QUIET/USDT"})
        client.get_top_volatile_symbols.assert_not_called()
        client.get_recent_sl_tp_fills.assert_called_once_with(
            "QUIET/USDT", 1788832800000,
            entry_meta={"side": "BUY", "trade_id": "owned"},
        )

    def test_closed_tracked_position_is_recorded_before_removing_tracking(self):
        alert = {"symbol": "QUIET/USDT", "type": "STOP_LOSS", "price": 99, "pnl": -1}
        client, state, notifier, close = self.run_scan(active={"QUIET/USDT"}, alerts=[alert])
        close.assert_called_once_with(alert, state, client=client, notifier=notifier)
        state.remove_active_symbol.assert_called_once_with("QUIET/USDT")
        calls = [item[0] for item in state.mock_calls]
        self.assertLess(calls.index("record_close"), calls.index("remove_active_symbol"))
        notifier.send_pnl_alert.assert_called_once()

    def test_unknown_exchange_position_does_not_record_or_remove_close(self):
        alert = {"symbol": "QUIET/USDT", "type": "STOP_LOSS", "price": 99, "pnl": -1}
        _, state, _, close = self.run_scan(
            active={"QUIET/USDT"}, alerts=[alert], position_amount=None,
        )
        close.assert_not_called()
        state.remove_active_symbol.assert_not_called()

    def test_risk_failure_still_blocks_scanner_before_fill_polling(self):
        client, _, _, _ = self.run_scan(active={"QUIET/USDT"}, risk_status="UNKNOWN")
        client.get_top_volatile_symbols.assert_not_called()
        client.get_recent_sl_tp_fills.assert_not_called()


if __name__ == "__main__":
    unittest.main()
