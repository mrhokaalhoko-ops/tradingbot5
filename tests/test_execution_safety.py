import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pandas as pd

from core.config import Config
from core.exchange import ExchangeClient
from engines.social_engine import SocialEngine
import main as main_module
from main import enforce_daily_loss_limit, recover_pending_order_intents
from strategy.risk_manager import RiskManager
from strategy.trailing_stop import TrailingStopManager
from webapp.server import _verify_telegram_init_data
from utils.telegram_bot import _fetch_live_positions_for_status
import utils.state_manager as state_module
from utils.state_manager import StateManager


class TestExchangeProtectionSafety(unittest.TestCase):
    def _client(self):
        client = ExchangeClient.__new__(ExchangeClient)
        client.exchange = Mock()
        client._is_hedge_mode = False
        client.exchange.price_to_precision.side_effect = lambda _symbol, price: str(price)
        client.exchange.market.return_value = {"id": "BTCUSDT"}
        client.exchange.fapiPrivateGetOpenAlgoOrders.return_value = []
        client.exchange.fapiPrivatePostAlgoOrder.side_effect = lambda params: {
            "algoId": f"algo-{params.get('clientAlgoId', 'protection')}",
            "clientAlgoId": params.get("clientAlgoId"),
            "algoStatus": "NEW",
            "orderType": params.get("type"),
            "side": params.get("side"),
            "triggerPrice": params.get("triggerPrice"),
            "positionSide": params.get("positionSide", "BOTH"),
        }
        return client

    def test_trade_excursions_use_original_stop_risk(self):
        meta = {
            "side": "BUY",
            "entry_price": 100.0,
            "initial_sl": 95.0,
            "current_sl": 102.0,
            "mfe_r": 0.0,
            "mae_r": 0.0,
        }
        self.assertTrue(main_module.update_trade_excursions(meta, 107.5))
        self.assertAlmostEqual(meta["mfe_r"], 1.5)
        self.assertTrue(main_module.update_trade_excursions(meta, 97.5))
        self.assertAlmostEqual(meta["mae_r"], 0.5)
        self.assertAlmostEqual(meta["mfe_r"], 1.5)

    def test_sell_excursions_are_direction_normalized(self):
        meta = {"side": "SELL", "entry_price": 100.0, "initial_sl": 104.0}
        main_module.update_trade_excursions(meta, 94.0)
        main_module.update_trade_excursions(meta, 102.0)
        self.assertAlmostEqual(meta["mfe_r"], 1.5)
        self.assertAlmostEqual(meta["mae_r"], 0.5)

    def test_replace_sl_cancels_old_before_creating_new(self):
        client = self._client()
        events = []
        client.exchange.fetch_open_orders.return_value = [{
            "id": "old-sl",
            "type": "stop_market",
            "info": {"orderType": "STOP_MARKET", "stopPrice": "98.0"},
        }]
        client.exchange.fapiPrivatePostAlgoOrder.side_effect = (
            lambda params: events.append("create") or {
                "algoId": "new-sl", "clientAlgoId": params["clientAlgoId"],
                "algoStatus": "NEW", "orderType": params["type"],
                "side": params["side"], "triggerPrice": params["triggerPrice"],
            }
        )
        client.exchange.cancel_order.side_effect = lambda *a, **k: events.append("cancel")

        result = client.replace_sl_order("BTC/USDT", "BUY", 0.1, 99.5)

        self.assertEqual(result, 99.5)
        self.assertEqual(events, ["cancel", "create"])

    def test_replace_sl_restores_old_when_new_order_fails(self):
        client = self._client()
        client.exchange.fetch_open_orders.return_value = [{
            "id": "old-sl", "info": {"orderType": "STOP_MARKET", "stopPrice": "98.0"}
        }]
        attempted_prices = []

        def create_order(params):
            attempted_prices.append(float(params["triggerPrice"]))
            if len(attempted_prices) == 1:
                raise RuntimeError("Binance rejected new stop")
            return {
                "algoId": "restored-old-sl",
                "clientAlgoId": params["clientAlgoId"],
                "algoStatus": "NEW", "orderType": params["type"],
                "side": params["side"], "triggerPrice": params["triggerPrice"],
            }

        client.exchange.fapiPrivatePostAlgoOrder.side_effect = create_order

        self.assertFalse(client.replace_sl_order("BTC/USDT", "BUY", 0.1, 99.5))
        client.exchange.cancel_order.assert_called_once()
        self.assertEqual(attempted_prices, [99.5, 98.0])

        # المحاولة التالية داخل backoff لا ترسل أي طلب جديد إلى Binance.
        self.assertFalse(client.replace_sl_order("BTC/USDT", "BUY", 0.1, 99.7))
        self.assertEqual(attempted_prices, [99.5, 98.0])

    def test_replace_sl_closes_position_if_new_and_restore_both_fail(self):
        client = self._client()
        client.exchange.fetch_open_orders.return_value = [{
            "id": "old-sl", "info": {"orderType": "STOP_MARKET", "stopPrice": "98.0"}
        }]
        client.exchange.fapiPrivatePostAlgoOrder.side_effect = RuntimeError("algo unavailable")
        client.close_position = Mock(return_value=True)

        self.assertFalse(client.replace_sl_order("BTC/USDT", "BUY", 0.1, 99.5))

        client.close_position.assert_called_once_with("BTC/USDT", "buy", 0.1)

    def test_partial_sl_cancel_failure_closes_if_protection_cannot_be_restored(self):
        client = self._client()
        first = {"id": "old-1", "info": {"orderType": "STOP_MARKET", "stopPrice": "98"}}
        second = {"id": "old-2", "info": {"orderType": "STOP_MARKET", "stopPrice": "97"}}
        client._fetch_position_protection_orders = Mock(side_effect=[
            {"status": "OK", "sl_orders": [first, second], "tp_orders": []},
            {"status": "OK", "sl_orders": [], "tp_orders": []},
        ])
        client._cancel_protection_order = Mock(
            side_effect=[None, RuntimeError("cancel timeout")]
        )
        client.place_sl_only = Mock(return_value=False)
        client.close_position = Mock(return_value=True)

        result = client.replace_sl_order("BTC/USDT", "BUY", 0.1, 99.0)

        self.assertFalse(result)
        client.close_position.assert_called_once_with("BTC/USDT", "buy", 0.1)

    def test_replace_sl_in_hedge_mode_cancels_only_same_position_side(self):
        client = self._client()
        client._is_hedge_mode = True
        client.exchange.fetch_open_orders.return_value = [
            {"id": "long-sl", "info": {"orderType": "STOP_MARKET", "positionSide": "LONG", "stopPrice": "98.0"}},
            {"id": "short-sl", "info": {"orderType": "STOP_MARKET", "positionSide": "SHORT", "stopPrice": "102.0"}},
        ]
        client.exchange.fapiPrivatePostAlgoOrder.side_effect = lambda params: {
            "algoId": "new-long-sl", "clientAlgoId": params["clientAlgoId"],
            "algoStatus": "NEW", "orderType": params["type"],
            "side": params["side"], "triggerPrice": params["triggerPrice"],
            "positionSide": params.get("positionSide", "BOTH"),
        }

        self.assertEqual(client.replace_sl_order("BTC/USDT", "BUY", 0.1, 99.5), 99.5)

        cancelled = [call.args[0] for call in client.exchange.cancel_order.call_args_list]
        self.assertEqual(cancelled, ["long-sl"])

    def test_replace_sl_cancels_binance_algo_order_via_algo_endpoint(self):
        client = self._client()
        client.exchange.fapiPrivateGetOpenAlgoOrders.return_value = [{
            "algoId": "algo-old",
            "clientAlgoId": "old-client",
            "orderType": "STOP_MARKET",
            "triggerPrice": "98.0",
            "positionSide": "BOTH",
            "algoStatus": "NEW",
        }]
        client.exchange.fetch_open_orders.return_value = []
        client.exchange.fapiPrivatePostAlgoOrder.side_effect = lambda params: {
            "algoId": "new-stop", "clientAlgoId": params["clientAlgoId"],
            "algoStatus": "NEW", "orderType": params["type"],
            "side": params["side"], "triggerPrice": params["triggerPrice"],
        }

        result = client.replace_sl_order("BTC/USDT", "BUY", 0.1, 99.5)

        self.assertEqual(result, 99.5)
        client.exchange.fapiPrivateDeleteAlgoOrder.assert_called_once_with({
            "symbol": "BTCUSDT", "algoId": "algo-old"
        })
        client.exchange.cancel_order.assert_not_called()

    def test_durable_sl_replacement_recovers_crash_after_cancel_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_file = state_module.STATE_FILE
            state_module.STATE_FILE = os.path.join(temp_dir, "state.json")
            try:
                state = StateManager()
                client = self._client()
                client.bind_state_manager(state)
                old_order = {
                    "id": "old-sl",
                    "info": {"orderType": "STOP_MARKET", "stopPrice": "98.0"},
                }
                old_snapshot = {
                    "status": "OK", "sl_orders": [old_order], "tp_orders": [],
                }
                client._fetch_position_protection_orders = Mock(
                    side_effect=[old_snapshot, old_snapshot]
                )
                client.get_position_status = Mock(return_value={
                    "status": "OPEN", "amount": 0.1, "positions": [],
                })
                client._cancel_protection_order = Mock(side_effect=SystemExit("crash"))

                with self.assertRaises(SystemExit):
                    client.replace_sl_order("BTC/USDT", "BUY", 0.1, 99.0)

                persisted = state.get_protection_replacement("BTC/USDT", "LONG")
                self.assertEqual(persisted["phase"], "CANCEL_IN_PROGRESS")

                restarted = StateManager()
                client2 = self._client()
                client2.bind_state_manager(restarted)
                new_order = {
                    "id": "new-sl",
                    "info": {"orderType": "STOP_MARKET", "stopPrice": "99.0"},
                }
                client2._fetch_position_protection_orders = Mock(side_effect=[
                    {"status": "OK", "sl_orders": [], "tp_orders": []},
                    {"status": "OK", "sl_orders": [new_order], "tp_orders": []},
                ])
                client2.get_position_status = Mock(return_value={
                    "status": "OPEN", "amount": 0.1, "positions": [],
                })
                client2.exchange.fapiPrivatePostAlgoOrder.side_effect = lambda params: {
                    "algoId": "new-sl", "clientAlgoId": params["clientAlgoId"],
                    "algoStatus": "NEW", "orderType": params["type"],
                    "side": params["side"], "triggerPrice": params["triggerPrice"],
                }

                self.assertFalse(client2.recover_sl_replacement("BTC/USDT"))
                with patch("core.exchange.time.time", return_value=time.time() + 5):
                    self.assertEqual(
                        client2.recover_sl_replacement("BTC/USDT"), 99.0
                    )

                self.assertEqual(
                    restarted.get_protection_replacement("BTC/USDT", "LONG"), {}
                )
                self.assertEqual(
                    client2.exchange.fapiPrivatePostAlgoOrder.call_count, 1
                )
            finally:
                state_module.STATE_FILE = old_state_file

    def test_durable_sl_replacement_blocks_multiple_stops_without_cancel(self):
        state = Mock()
        saga = {
            "symbol": "BTC/USDT", "position_side": "LONG", "side": "BUY",
            "amount": 0.1, "old_stop": 98.0, "new_stop": 99.0,
        }
        state.get_protection_replacement.return_value = saga
        client = self._client()
        client.bind_state_manager(state)
        client.get_position_status = Mock(return_value={
            "status": "OPEN", "amount": 0.1, "positions": [],
        })
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK",
            "sl_orders": [
                {"id": "one", "info": {"stopPrice": "98"}},
                {"id": "two", "info": {"stopPrice": "97"}},
            ],
            "tp_orders": [],
        })
        client._cancel_protection_order = Mock()

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))

        client._cancel_protection_order.assert_not_called()
        self.assertEqual(
            state.transition_protection_replacement.call_args.args[3],
            "BLOCKED_MULTIPLE_STOPS",
        )

    def test_durable_sl_create_timeout_adopts_existing_client_id(self):
        state = Mock()
        state.transition_protection_replacement.return_value = True
        saga = {
            "symbol": "BTC/USDT", "position_side": "LONG", "side": "BUY",
            "amount": 0.1, "old_stop": 98.0, "new_stop": 99.0,
            "new_client_order_id": "stable-new-sl", "phase": "OLD_ABSENT",
        }
        client = self._client()
        client.bind_state_manager(state)
        client.get_position_status = Mock(return_value={
            "status": "OPEN", "amount": 0.1, "positions": [],
        })
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client._create_close_position_protection = Mock(
            side_effect=TimeoutError("send status unknown")
        )
        client.resolve_algo_order_by_client_id = Mock(return_value={
            "status": "FOUND", "order": {"id": "adopted", "status": "NEW"},
        })

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))

        phases = [
            call.args[3] for call in state.transition_protection_replacement.call_args_list
        ]
        self.assertIn("NEW_ACKNOWLEDGED", phases)
        client.resolve_algo_order_by_client_id.assert_called_once_with(
            "BTC/USDT", "stable-new-sl"
        )

    def test_durable_sl_failure_without_restorable_stop_requests_one_close(self):
        state = Mock()
        state.transition_protection_replacement.return_value = True
        saga = {
            "symbol": "BTC/USDT", "position_side": "LONG", "side": "BUY",
            "amount": 0.1, "old_stop": 0.0, "new_stop": 99.0,
            "new_client_order_id": "new-sl", "phase": "OLD_ABSENT",
        }
        client = self._client()
        client.bind_state_manager(state)
        client.get_position_status = Mock(return_value={
            "status": "OPEN", "amount": 0.1, "positions": [],
        })
        client._fetch_position_protection_orders = Mock(return_value={
            "status": "OK", "sl_orders": [], "tp_orders": [],
        })
        client._submit_or_resolve_protection = Mock(return_value={
            "status": "FAILED_DEFINITIVE", "reason": "REJECTED",
        })
        client.close_position = Mock(return_value=False)

        self.assertFalse(client.recover_sl_replacement("BTC/USDT", saga=saga))

        client.close_position.assert_called_once_with("BTC/USDT", "buy", 0.1)
        self.assertEqual(
            state.transition_protection_replacement.call_args_list[-1].args[3],
            "EMERGENCY_CLOSE_REQUESTED",
        )

    def test_cancel_position_protection_keeps_opposite_hedge_leg(self):
        client = self._client()
        client._is_hedge_mode = True
        orders = [
            {"id": "long-sl", "info": {"orderType": "STOP_MARKET", "positionSide": "LONG"}},
            {"id": "long-tp", "info": {"orderType": "TAKE_PROFIT_MARKET", "positionSide": "LONG"}},
            {"id": "short-sl", "info": {"orderType": "STOP_MARKET", "positionSide": "SHORT"}},
        ]
        client.exchange.fetch_open_orders.side_effect = [orders, []]

        self.assertTrue(client.cancel_position_protection_orders("BTC/USDT", "LONG"))

        cancelled = {call.args[0] for call in client.exchange.cancel_order.call_args_list}
        self.assertEqual(cancelled, {"long-sl", "long-tp"})
        self.assertNotIn("short-sl", cancelled)

    def test_close_does_not_cancel_protection_until_zero_confirmed(self):
        client = self._client()
        client.get_position_status = Mock(side_effect=[
            {"status": "OPEN", "amount": 0.1},
            {"status": "CLOSED", "amount": 0.0},
        ])
        client.cancel_all_orders = Mock(return_value=True)

        self.assertTrue(client.close_position("BTC/USDT", "BUY", 0.1))
        client.exchange.create_order.assert_called_once()
        client.cancel_all_orders.assert_called_once_with("BTC/USDT")

    def test_close_failure_leaves_protection_in_place(self):
        client = self._client()
        client.get_position_status = Mock(return_value={"status": "OPEN", "amount": 0.1})
        client.cancel_all_orders = Mock(return_value=True)

        self.assertFalse(client.close_position("BTC/USDT", "BUY", 0.1))
        client.cancel_all_orders.assert_not_called()

    def test_close_intent_is_persisted_before_market_send(self):
        client = self._client()
        events = []
        state = Mock()
        state.get_close_intent.return_value = {}
        state.get_entry_metadata.return_value = {
            "trade_id": "position-1", "pending_close_reason": "BOT_EXIT"
        }
        state.prepare_close_intent.side_effect = lambda **_kwargs: (
            events.append("persist")
            or {"status": "PREPARED", "client_order_id": "close-client-1"}
        )
        state.transition_close_intent.return_value = True
        state.archive_close_intent.return_value = True
        client._state_manager = state
        client.exchange.create_order.side_effect = lambda *_a, **_k: (
            events.append("send") or {"id": "close-order", "status": "closed"}
        )
        client.get_position_status = Mock(return_value={"status": "CLOSED", "amount": 0})
        client.cancel_all_orders = Mock(return_value=True)

        self.assertTrue(client.close_position("BTC/USDT", "BUY", 0.1))

        self.assertEqual(events, ["persist", "send"])
        params = client.exchange.create_order.call_args.args[5]
        self.assertEqual(params["newClientOrderId"], "close-client-1")
        state.archive_close_intent.assert_called_once()

    def test_close_timeout_and_unknown_position_never_send_duplicate(self):
        client = self._client()
        intent = {
            "status": "UNKNOWN", "client_order_id": "close-client-unknown",
            "created_at": time.time(), "amount": 0.1,
        }
        state = Mock()
        state.get_close_intent.side_effect = [{}, intent]
        state.get_entry_metadata.return_value = {"trade_id": "position-2"}
        state.prepare_close_intent.return_value = intent
        state.transition_close_intent.return_value = True
        client._state_manager = state
        client.exchange.create_order.side_effect = RuntimeError("503 unknown")
        client.resolve_order_by_client_id = Mock(return_value={
            "status": "UNKNOWN", "reason": "RequestTimeout"
        })
        client.cancel_all_orders = Mock(return_value=True)

        self.assertFalse(client.close_position("BTC/USDT", "BUY", 0.1))
        client.get_position_status = Mock(return_value={"status": "UNKNOWN", "amount": 0})
        self.assertFalse(client.close_position("BTC/USDT", "BUY", 0.1))

        self.assertEqual(client.exchange.create_order.call_count, 1)
        client.cancel_all_orders.assert_not_called()

    def test_order_resolution_distinguishes_not_found_from_unknown(self):
        client = self._client()
        client.exchange.fapiPrivateGetOrder.side_effect = RuntimeError(
            'binance {"code":-2013,"msg":"Order does not exist."}'
        )
        self.assertEqual(
            client.resolve_order_by_client_id("BTC/USDT", "entry-1")["status"],
            "NOT_FOUND",
        )

        client.exchange.fapiPrivateGetOrder.side_effect = RuntimeError(
            "Timeout waiting for response"
        )
        self.assertEqual(
            client.resolve_order_by_client_id("BTC/USDT", "entry-1")["status"],
            "UNKNOWN",
        )

    def test_fill_grouping_uses_stored_sl_id_and_includes_commission(self):
        client = self._client()
        client.exchange.fetch_my_trades.return_value = [
            {
                "id": "f1", "order": "sl-1", "side": "sell", "amount": 0.1,
                "price": 100.0, "timestamp": 1000,
                "info": {"positionSide": "LONG", "realizedPnl": "-1", "commission": "0.01", "commissionAsset": "USDT", "orderType": "STOP_MARKET"},
            },
            {
                "id": "f2", "order": "sl-1", "side": "sell", "amount": 0.2,
                "price": 99.0, "timestamp": 1001,
                "info": {"positionSide": "LONG", "realizedPnl": "-2", "commission": "0.02", "commissionAsset": "USDT", "orderType": "STOP_MARKET"},
            },
        ]

        fills = client.get_recent_sl_tp_fills("BTC/USDT", entry_meta={
            "side": "BUY", "position_side": "LONG", "sl_order_id": "sl-1"
        })

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["type"], "STOP_LOSS")
        self.assertAlmostEqual(fills[0]["qty"], 0.3)
        self.assertAlmostEqual(fills[0]["commission"], 0.03)
        self.assertAlmostEqual(fills[0]["pnl"], -3.03)

    def test_net_pnl_includes_entry_exit_fees_and_funding(self):
        client = self._client()
        client.exchange.fapiPrivateGetIncome.return_value = [{
            "asset": "USDT", "income": "-0.05", "incomeType": "FUNDING_FEE"
        }]
        client.exchange.fetch_my_trades.return_value = [
            {
                "id": "entry", "order": "entry-1", "side": "buy", "amount": 0.1,
                "price": 100.0, "timestamp": 1000,
                "info": {"positionSide": "LONG", "realizedPnl": "0", "commission": "0.01", "commissionAsset": "USDT", "orderType": "MARKET"},
            },
            {
                "id": "exit", "order": "tp-1", "side": "sell", "amount": 0.1,
                "price": 110.0, "timestamp": 2000,
                "info": {"positionSide": "LONG", "realizedPnl": "1", "commission": "0.02", "commissionAsset": "USDT", "orderType": "TAKE_PROFIT_MARKET"},
            },
        ]

        fills = client.get_recent_sl_tp_fills("BTC/USDT", since_ms=500, entry_meta={
            "side": "BUY", "position_side": "LONG", "tp_order_id": "tp-1"
        })

        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0]["realized_pnl_gross"], 1.0)
        self.assertAlmostEqual(fills[0]["entry_commission"], 0.01)
        self.assertAlmostEqual(fills[0]["exit_commission"], 0.02)
        self.assertAlmostEqual(fills[0]["funding"], -0.05)
        self.assertAlmostEqual(fills[0]["pnl"], 0.92)
        self.assertTrue(fills[0]["pnl_complete"])

    def test_user_trade_history_paginates_past_one_thousand_rows(self):
        client = self._client()
        first_page = [
            {"id": f"t{i}", "timestamp": 1_000 + i, "info": {}}
            for i in range(1000)
        ]
        second_page = [{"id": "t1000", "timestamp": 2_000, "info": {}}]
        client.exchange.fetch_my_trades.side_effect = [first_page, second_page]

        rows = client._fetch_my_trades_complete("BTC/USDT", since_ms=500)

        self.assertEqual(len(rows), 1001)
        self.assertEqual(client.exchange.fetch_my_trades.call_count, 2)
        self.assertEqual(
            client.exchange.fetch_my_trades.call_args_list[1].kwargs["since"],
            2000,
        )

    def test_funding_history_paginates_past_one_thousand_rows(self):
        client = self._client()
        first_page = [
            {
                "tranId": str(i), "time": 1_000 + i, "asset": "USDT",
                "income": "0.01",
            }
            for i in range(1000)
        ]
        second_page = [{
            "tranId": "1000", "time": 2_000, "asset": "USDT", "income": "0.01",
        }]
        client.exchange.fapiPrivateGetIncome.side_effect = [first_page, second_page]

        result = client._fetch_funding_income("BTC/USDT", since_ms=500)

        self.assertEqual(result["status"], "OK")
        self.assertAlmostEqual(result["funding"], 10.01)
        self.assertEqual(client.exchange.fapiPrivateGetIncome.call_count, 2)

    def test_profitable_market_close_is_manual_not_take_profit(self):
        client = self._client()
        client.exchange.fetch_my_trades.return_value = [{
            "id": "f1", "order": "manual-1", "side": "sell", "amount": 0.1,
            "price": 110.0, "timestamp": 1000,
            "info": {"positionSide": "LONG", "realizedPnl": "1", "orderType": "MARKET"},
        }]

        fills = client.get_recent_sl_tp_fills("BTC/USDT", entry_meta={
            "side": "BUY", "position_side": "LONG", "sl_order_id": "sl", "tp_order_id": "tp"
        })

        self.assertEqual(fills[0]["type"], "MANUAL_CLOSE")

    def test_algo_actual_order_id_classifies_stop_fill(self):
        client = self._client()
        client.exchange.fetch_my_trades.return_value = [{
            "id": "fill-1", "order": "987654", "side": "sell", "amount": 0.1,
            "price": 99.0, "timestamp": 1000,
            "info": {"positionSide": "LONG", "realizedPnl": "-1", "orderType": "MARKET"},
        }]
        client.exchange.fapiPrivateGetAlgoOrder.return_value = {
            "algoId": "123", "clientAlgoId": "trade_sl", "actualOrderId": "987654",
            "algoStatus": "TRIGGERED", "orderType": "STOP_MARKET",
        }

        fills = client.get_recent_sl_tp_fills("BTC/USDT", entry_meta={
            "side": "BUY", "position_side": "LONG", "sl_client_id": "trade_sl",
        })

        self.assertEqual(fills[0]["type"], "STOP_LOSS")
        self.assertEqual(fills[0]["classification_source"], "protection_order_match")

    def test_fresh_persisted_bot_close_intent_classifies_market_fill(self):
        client = self._client()
        client.exchange.fetch_my_trades.return_value = [{
            "id": "fill-2", "order": "market-close", "side": "sell", "amount": 0.1,
            "price": 110.0, "timestamp": 1000,
            "info": {"positionSide": "LONG", "realizedPnl": "1", "orderType": "MARKET"},
        }]

        fills = client.get_recent_sl_tp_fills("BTC/USDT", entry_meta={
            "side": "BUY",
            "position_side": "LONG",
            "pending_close_reason": "BOT_TRAILING_STOP_BREACH",
            "pending_close_requested_at": 1.0,
        })

        self.assertEqual(fills[0]["type"], "BOT_TRAILING_STOP_BREACH")
        self.assertEqual(fills[0]["classification_source"], "persisted_bot_close_intent")


class TestOrderIntentRecoveryOwnership(unittest.TestCase):
    def test_live_owner_intent_inside_grace_is_not_touched(self):
        state = Mock()
        state.get_order_intents.return_value = {"BTC/USDT": {
            "status": "PROTECTING",
            "owner_instance_id": main_module._PROCESS_INSTANCE_ID,
            "created_at": time.time() - 5,
            "updated_at": time.time(),
        }}
        client = Mock()

        recover_pending_order_intents(
            client, state, Mock(), Mock(), Mock()
        )

        client.get_position_status.assert_not_called()
        state.transition_order_intent.assert_not_called()

    def test_recovery_adopts_existing_protection_without_duplicate_place(self):
        intent = {
            "trade_id": "recover-1",
            "status": "SUBMITTING",
            "owner_instance_id": "old-process",
            "created_at": time.time() - 300,
            "updated_at": time.time() - 300,
            "side": "BUY",
            "position_side": "LONG",
            "expected_entry": 100.0,
            "sl": 95.0,
            "tp": 120.0,
        }
        state = Mock()
        state.get_order_intents.return_value = {"BTC/USDT": intent}
        state.transition_order_intent.return_value = True
        state.get_order_intent.return_value = {**intent, "status": "RECOVERING"}
        state.save_entry_metadata.return_value = True

        client = Mock()
        client._is_hedge_mode = False
        client.get_position_status.return_value = {
            "status": "OPEN", "amount": 0.1,
            "positions": [{"entry_price": 101.0}],
        }
        client.ensure_position_protection.return_value = {
            "status": "OK", "sl_ok": True, "tp_ok": True,
            "sl_order_id": "existing-sl", "tp_order_id": "existing-tp",
        }
        risk = Mock()
        risk.get_tp_steps.return_value = []

        recover_pending_order_intents(
            client, state, Mock(), Mock(), risk
        )

        client.ensure_position_protection.assert_called_once()
        client.place_sl_tp.assert_not_called()
        state.save_entry_metadata.assert_called_once()
        state.remove_order_intent.assert_called_once_with("BTC/USDT")

    def test_unknown_protection_query_never_triggers_emergency_close(self):
        intent = {
            "trade_id": "recover-unknown",
            "status": "SUBMITTING",
            "owner_instance_id": "old-process",
            "created_at": time.time() - 300,
            "updated_at": time.time() - 300,
            "side": "BUY",
            "position_side": "LONG",
            "expected_entry": 100.0,
            "sl": 95.0,
            "tp": 120.0,
        }
        state = Mock()
        state.get_order_intents.return_value = {"BTC/USDT": intent}
        state.transition_order_intent.return_value = True
        state.get_order_intent.return_value = {**intent, "status": "RECOVERING"}
        client = Mock()
        client._is_hedge_mode = False
        client.get_position_status.return_value = {
            "status": "OPEN", "amount": 0.1,
            "positions": [{"entry_price": 100.0}],
        }
        client.ensure_position_protection.return_value = {
            "status": "UNKNOWN", "sl_ok": False, "tp_ok": False,
        }

        recover_pending_order_intents(
            client, state, Mock(), Mock(), Mock()
        )

        client.close_position.assert_not_called()
        state.save_entry_metadata.assert_not_called()
        transitions = state.transition_order_intent.call_args_list
        self.assertEqual(transitions[-1].args[2], "RECOVERY_WAIT_PROTECTION_QUERY")

    def test_closed_position_with_unknown_order_query_keeps_intent(self):
        intent = {
            "trade_id": "recover-unknown-order",
            "status": "SUBMITTING",
            "owner_instance_id": "old-process",
            "created_at": time.time() - 600,
            "updated_at": time.time() - 600,
            "side": "BUY",
            "position_side": "LONG",
        }
        state = Mock()
        state.get_order_intents.return_value = {"BTC/USDT": intent}
        state.transition_order_intent.return_value = True
        state.get_order_intent.return_value = {**intent, "status": "RECOVERING"}
        client = Mock()
        client._is_hedge_mode = False
        client.get_position_status.return_value = {"status": "CLOSED", "amount": 0}
        client.resolve_order_by_client_id.return_value = {
            "status": "UNKNOWN", "reason": "RequestTimeout"
        }

        recover_pending_order_intents(client, state, Mock(), Mock(), Mock())

        state.remove_order_intent.assert_not_called()
        state.archive_order_intent.assert_not_called()
        self.assertEqual(
            state.transition_order_intent.call_args_list[-1].args[2],
            "RECOVERY_WAIT_ORDER_RESOLUTION",
        )

    def test_terminal_rejected_order_is_archived_with_proof(self):
        intent = {
            "trade_id": "recover-rejected",
            "status": "SUBMITTING",
            "owner_instance_id": "old-process",
            "created_at": time.time() - 600,
            "updated_at": time.time() - 600,
            "side": "BUY",
            "position_side": "LONG",
        }
        state = Mock()
        state.get_order_intents.return_value = {"BTC/USDT": intent}
        state.transition_order_intent.return_value = True
        state.get_order_intent.return_value = {**intent, "status": "RECOVERING"}
        client = Mock()
        client._is_hedge_mode = False
        client.get_position_status.return_value = {"status": "CLOSED", "amount": 0}
        client.resolve_order_by_client_id.return_value = {
            "status": "FOUND",
            "order": {"status": "rejected", "filled": 0, "amount": 0.1},
        }

        recover_pending_order_intents(client, state, Mock(), Mock(), Mock())

        state.archive_order_intent.assert_called_once()
        self.assertEqual(state.archive_order_intent.call_args.args[1], "ENTRY_TERMINAL_NO_FILL")
        state.remove_order_intent.assert_not_called()

    def test_duplicate_close_returns_explicit_in_progress_status(self):
        symbol = "BTC/USDT"
        with main_module._closing_guard:
            main_module._closing_lock.add(symbol)
        try:
            result = main_module.handle_trade_close(
                {"symbol": symbol}, Mock(), client=Mock(), notifier=Mock()
            )
        finally:
            with main_module._closing_guard:
                main_module._closing_lock.discard(symbol)

        self.assertEqual(result, main_module.CLOSE_ALREADY_PROCESSING)


class _RiskState:
    def __init__(self, initial=100.0):
        self.values = {"risk_equity_status": "OK", "kill_switch_active": False}
        self.initial = initial

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def update_daily_balance(self, _equity):
        return None

    def get_initial_balance(self):
        return self.initial


class TestRiskFailClosed(unittest.TestCase):
    def test_balance_failure_is_unknown_and_does_not_use_reference_balance(self):
        client = Mock()
        client.get_balance.return_value = None
        state = _RiskState()

        result = enforce_daily_loss_limit(client, state)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(state.get("risk_equity_status"), "UNKNOWN")

    def test_daily_drawdown_activates_kill_switch(self):
        client = Mock()
        client.get_balance.return_value = {
            "USDT": {"total": 95.0, "free": 90.0},
            "info": {"totalMarginBalance": "95", "availableBalance": "90"},
        }
        state = _RiskState(initial=100.0)
        old_limit = Config.DAILY_LOSS_LIMIT_PERCENT
        Config.DAILY_LOSS_LIMIT_PERCENT = 3.0
        try:
            result = enforce_daily_loss_limit(client, state)
        finally:
            Config.DAILY_LOSS_LIMIT_PERCENT = old_limit

        self.assertEqual(result["status"], "OK")
        self.assertTrue(state.get("kill_switch_active"))
        self.assertEqual(result["available"], 90.0)


class TestStartupTradingModeSafety(unittest.TestCase):
    def test_testnet_never_requires_live_confirmation(self):
        with patch.object(Config, "USE_TESTNET", True):
            with patch.dict(os.environ, {}, clear=True):
                self.assertTrue(main_module.validate_live_trading_confirmation())

    def test_live_mode_requires_exact_confirmation(self):
        with patch.object(Config, "USE_TESTNET", False):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    main_module.validate_live_trading_confirmation()

            with patch.dict(
                os.environ,
                {"LIVE_TRADING_CONFIRMATION": "I_ACCEPT_REAL_MONEY_RISK"},
                clear=True,
            ):
                self.assertTrue(main_module.validate_live_trading_confirmation())

    def test_live_guard_runs_before_exchange_client_is_created(self):
        with patch.object(
            main_module,
            "validate_live_trading_confirmation",
            side_effect=RuntimeError("blocked"),
        ):
            with patch.object(main_module, "ExchangeClient") as exchange_cls:
                with self.assertRaises(RuntimeError):
                    main_module.main()

        exchange_cls.assert_not_called()


class TestTelegramStatusFailClosed(unittest.TestCase):
    def test_position_api_failure_does_not_erase_local_active_symbols(self):
        client = Mock()
        client.exchange.fetch_positions.side_effect = RuntimeError("timeout")
        state = Mock()

        result = _fetch_live_positions_for_status(client, state)

        self.assertIsNone(result)
        state.save_active_symbols.assert_not_called()

    def test_successful_empty_snapshot_may_clear_local_active_symbols(self):
        client = Mock()
        client.exchange.fetch_positions.return_value = []
        state = Mock()

        result = _fetch_live_positions_for_status(client, state)

        self.assertEqual(result, [])
        state.save_active_symbols.assert_called_once_with(set())


class TestFullPositionStepTrail(unittest.TestCase):
    def test_step_trail_keeps_full_amount(self):
        manager = TrailingStopManager()
        manager.register_trade("BTC/USDT", "BUY", 100.0, 95.0, amount=2.0)

        manager.mark_step_trail_active("BTC/USDT", 101.0, 2.0)
        trade = manager.get_trade_info("BTC/USDT")

        self.assertEqual(trade["amount"], 2.0)
        self.assertEqual(trade["full_amount"], 2.0)
        self.assertEqual(trade["initial_sl"], 95.0)
        self.assertEqual(trade["current_sl"], 101.0)


class TestProtectionReplacementStateContract(unittest.TestCase):
    def _client(self):
        client = ExchangeClient.__new__(ExchangeClient)
        client.normalize_price = Mock(side_effect=lambda _symbol, value: float(value))
        client._stop_prices_equal = Mock(
            side_effect=lambda first, second: abs(float(first) - float(second)) < 1e-9
        )
        return client

    def test_acknowledged_durable_saga_is_pending_not_failed(self):
        client = self._client()
        state = Mock()
        state.get_protection_replacement.return_value = {
            "phase": "NEW_ACKNOWLEDGED",
            "replacement_id": "replace-1",
            "retry_after": 123.0,
        }
        client._state_manager = state
        client.replace_sl_order = Mock(return_value=False)

        result = client.replace_sl_order_state("BTC/USDT", "BUY", 0.1, 99.0)

        self.assertEqual(result["state"], "PENDING_VERIFICATION")
        self.assertEqual(result["reason"], "NEW_ACKNOWLEDGED")
        self.assertEqual(result["saga_id"], "replace-1")

    def test_terminal_saga_failure_is_explicit(self):
        client = self._client()
        state = Mock()
        state.get_protection_replacement.return_value = {
            "phase": "EMERGENCY_CLOSE_REQUESTED",
        }
        client._state_manager = state
        client.replace_sl_order = Mock(return_value=False)

        result = client.replace_sl_order_state("BTC/USDT", "BUY", 0.1, 99.0)

        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["reason"], "EMERGENCY_CLOSE_REQUESTED")

    def test_confirmed_and_noop_states_preserve_exchange_stop(self):
        client = self._client()
        client._state_manager = None
        client.replace_sl_order = Mock(return_value=99.0)
        confirmed = client.replace_sl_order_state("BTC/USDT", "BUY", 0.1, 99.0)
        self.assertEqual(confirmed["state"], "CONFIRMED")
        self.assertEqual(confirmed["confirmed_stop"], 99.0)

        client.replace_sl_order.return_value = 99.5
        noop = client.replace_sl_order_state("BTC/USDT", "BUY", 0.1, 99.0)
        self.assertEqual(noop["state"], "NOOP_PROTECTED")
        self.assertEqual(noop["confirmed_stop"], 99.5)

    def test_pending_trailing_replacement_is_not_counted_as_failure(self):
        manager = TrailingStopManager()
        manager.register_trade("BTC/USDT", "BUY", 100.0, 95.0, amount=1.0)
        manager.calculate_atr = Mock(return_value=1.0)
        manager.get_confirmed_swing = Mock(return_value=None)
        client = Mock()
        client.fetch_ohlcv.return_value = [
            [index, 100.0, 101.0, 99.0, 100.5, 1.0] for index in range(30)
        ]
        client.normalize_price.side_effect = lambda _symbol, value: float(value)
        client.get_position_amount.return_value = 1.0
        client.replace_sl_order_state = Mock(return_value={
            "state": "PENDING_VERIFICATION",
            "reason": "NEW_ACKNOWLEDGED",
            "retry_after": time.time() + 10,
        })

        self.assertIsNone(manager.update("BTC/USDT", 106.0, client))

        trade = manager.get_trade_info("BTC/USDT")
        self.assertEqual(trade["current_sl"], 95.0)
        self.assertEqual(trade.get("_failed_sl_count", 0), 0)
        self.assertNotIn("_failed_sl", trade)
        self.assertGreater(trade.get("_sl_retry_after", 0), time.time())

    def test_async_confirmation_synchronizes_without_regression(self):
        state = Mock()
        state.get_trailing_trades.return_value = {}
        state.get_entry_metadata.return_value = {}
        state.save_trailing_trades.return_value = True
        manager = TrailingStopManager(state_manager=state)
        manager.register_trade("BTC/USDT", "BUY", 100.0, 95.0, amount=1.0)
        trade = manager.get_trade_info("BTC/USDT")
        trade["_failed_sl"] = 98.0
        trade["_failed_sl_count"] = 2
        trade["_sl_retry_after"] = time.time() + 30

        self.assertTrue(manager.sync_confirmed_stop("BTC/USDT", 99.0))
        self.assertEqual(manager.get_current_sl("BTC/USDT"), 99.0)
        self.assertEqual(trade["_failed_sl_count"], 0)
        self.assertNotIn("_failed_sl", trade)
        self.assertNotIn("_sl_retry_after", trade)

        self.assertFalse(manager.sync_confirmed_stop("BTC/USDT", 98.0))
        self.assertEqual(manager.get_current_sl("BTC/USDT"), 99.0)


class TestUnifiedEntryExecutionGate(unittest.TestCase):
    @staticmethod
    def _execution_dependencies(order, position_status):
        state = Mock()
        state.get.side_effect = lambda key, default=None: default
        state.is_state_recovery_blocked.return_value = False
        state.get_order_intent.return_value = {}
        state.get_max_open_trades.return_value = 5
        state.get_active_symbols.return_value = set()
        state.save_order_intent.return_value = True
        state.transition_order_intent.return_value = True
        state.save_entry_metadata.return_value = True
        client = Mock()
        client.has_open_position.return_value = False
        client.exchange.fetch_ticker.return_value = {
            "bid": 99.99, "ask": 100.0, "last": 100.0,
        }
        client.execute_trade.return_value = order
        client.get_position_status.return_value = position_status
        client.place_sl_tp.return_value = {
            "sl_ok": True, "tp_ok": True,
            "sl_order_id": "sl", "tp_order_id": "tp",
        }
        client.get_balance.return_value = {
            "USDT": {"total": 1000.0, "free": 900.0}, "info": {}
        }
        risk = Mock()
        risk.calculate_sl_tp.return_value = (95.0, 120.0)
        risk.get_tp_steps.return_value = []
        risk.last_plan = {}
        risk.last_block_reason = ""
        return state, client, risk

    def test_ack_new_is_not_treated_as_fill(self):
        state, client, risk = self._execution_dependencies(
            {"id": "ack", "status": "open", "filled": 0, "amount": 0.1},
            {"status": "CLOSED", "amount": 0, "positions": []},
        )
        coin = {
            "entry_confirmation": {"passed": True, "reason": "PASS"},
            "optimal_entry": 100.0,
        }
        with patch.object(main_module, "audit_decision_event"):
            result = main_module.execute_and_protect_trade(
                "BTC/USDT", "buy", 0.1, 100.0, 5, 0.0025, 2.0,
                coin, None, None, {}, "buy", client, risk, Mock(), state,
                Mock(), set(), [], trade_id="entry-ack",
            )

        self.assertFalse(result)
        client.place_sl_tp.assert_not_called()
        transitions = state.transition_order_intent.call_args_list
        self.assertEqual(transitions[-1].args[2], "ENTRY_ACKNOWLEDGED_NO_FILL")
        state.remove_order_intent.assert_not_called()

    def test_local_pre_submit_failure_archives_intent_for_safe_sniper_retry(self):
        state, client, risk = self._execution_dependencies(
            None,
            {"status": "CLOSED", "amount": 0, "positions": []},
        )
        client.last_execution_failure = {
            "code": "USDM_CONTRACT_METADATA_UNAVAILABLE",
            "symbol": "FHE/USDT",
            "send_attempted": False,
            "detail": "",
        }
        state.archive_order_intent.return_value = True
        coin = {
            "entry_confirmation": {"passed": True, "reason": "PASS"},
            "optimal_entry": 0.02129,
        }
        client.exchange.fetch_ticker.return_value = {
            "bid": 0.02128, "ask": 0.02129, "last": 0.02129,
        }

        with patch.object(main_module, "audit_decision_event") as audit:
            result = main_module.execute_and_protect_trade(
                "FHE/USDT", "buy", 1000.0, 0.02129, 5, 0.0025, 0.0001,
                coin, None, None, {}, "buy", client, risk, Mock(), state,
                Mock(), set(), [], trade_id="entry-local-failure",
            )

        self.assertFalse(result)
        state.archive_order_intent.assert_called_once()
        self.assertEqual(
            state.archive_order_intent.call_args.args[1],
            "ENTRY_LOCAL_PRECHECK_FAILED",
        )
        self.assertTrue(any(
            call.kwargs.get("status") == "ENTRY_LOCAL_PRECHECK_FAILED"
            and call.kwargs.get("details", {}).get("intent_archived") is True
            for call in audit.call_args_list
        ))

    def test_partial_fill_protects_only_executed_quantity(self):
        state, client, risk = self._execution_dependencies(
            {
                "id": "partial", "status": "open", "filled": 0.04,
                "amount": 0.1, "average": 100.0,
                "info": {"status": "PARTIALLY_FILLED", "executedQty": "0.04"},
            },
            {"status": "UNKNOWN", "amount": 0, "positions": []},
        )
        coin = {
            "entry_confirmation": {"passed": True, "reason": "PASS"},
            "optimal_entry": 100.0,
        }
        with patch.object(main_module, "audit_decision_event"), patch.object(
            main_module.TradeJournal, "record_entry", return_value=None
        ), patch.object(main_module, "signal_dedup", None):
            result = main_module.execute_and_protect_trade(
                "BTC/USDT", "buy", 0.1, 100.0, 5, 0.0025, 2.0,
                coin, None, None, {}, "buy", client, risk, Mock(), state,
                Mock(), set(), [], trade_id="entry-partial",
            )

        self.assertTrue(result)
        self.assertAlmostEqual(client.place_sl_tp.call_args.args[2], 0.04)
        self.assertTrue(any(
            call.args[2] == "PARTIALLY_FILLED"
            for call in state.transition_order_intent.call_args_list
        ))

    def test_executable_price_basis_drift_is_rejected_before_order_submission(self):
        state, client, risk = self._execution_dependencies(
            None,
            {"status": "CLOSED", "amount": 0, "positions": []},
        )
        client.exchange.fetch_ticker.return_value = {
            "bid": 101.9, "ask": 102.0, "last": 102.0,
        }
        coin = {
            "entry_confirmation": {"passed": True, "reason": "PASS"},
            "optimal_entry": 100.0,
        }

        with patch.object(main_module, "audit_decision_event") as audit:
            result = main_module.execute_and_protect_trade(
                "BTC/USDT", "buy", 0.1, 100.0, 5, 0.0025, 2.0,
                coin, None, None, {}, "buy", client, risk, Mock(), state,
                Mock(), set(), [], trade_id="entry-basis-drift",
            )

        self.assertFalse(result)
        client.execute_trade.assert_not_called()
        state.save_order_intent.assert_not_called()
        self.assertTrue(any(
            call.kwargs.get("stage") == "EXECUTABLE_PRICE_GUARD"
            and call.kwargs.get("status") == "REJECTED"
            and call.kwargs.get("details", {}).get("reason")
            == "PRICE_BASIS_DEVIATION"
            for call in audit.call_args_list
        ))

    def test_jct_observed_mark_contract_divergence_is_rejected(self):
        result = main_module.evaluate_executable_entry_price(
            side="buy",
            ticker={
                "bid": 0.002165,
                "ask": 0.0021660088,
                "last": 0.002166,
            },
            reference_price=0.00213235,
            optimal_entry=0.0021315,
            atr_val=0.0000261076,
            max_spread_pct=0.001,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "PRICE_BASIS_DEVIATION")
        self.assertGreater(result["basis_delta_pct"], 0.015)
        self.assertGreater(result["basis_delta"], result["entry_tolerance"])

    def test_executable_price_must_be_inside_optimal_entry_zone(self):
        result = main_module.evaluate_executable_entry_price(
            side="buy",
            ticker={"bid": 100.95, "ask": 101.0, "last": 101.0},
            reference_price=100.9,
            optimal_entry=100.0,
            atr_val=0.5,
            max_spread_pct=0.001,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "EXECUTABLE_OUTSIDE_ENTRY_ZONE")

    def test_large_live_spike_cannot_reach_execution_outside_planned_entry(self):
        result = main_module.evaluate_executable_entry_price(
            side="buy",
            ticker={"bid": 104.99, "ask": 105.0, "last": 105.0},
            reference_price=105.0,
            optimal_entry=100.0,
            atr_val=1.0,
            max_spread_pct=0.001,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "EXECUTABLE_OUTSIDE_ENTRY_ZONE")
        self.assertAlmostEqual(result["entry_tolerance"], 0.25)
        self.assertGreater(
            abs(result["executable_price"] - result["optimal_entry"]),
            result["entry_tolerance"],
        )

    def test_confirmed_post_touch_reclaim_can_execute_when_repriced_rr_survives(self):
        result = main_module.evaluate_executable_entry_price(
            side="buy",
            ticker={"bid": 101.19, "ask": 101.2, "last": 101.2},
            reference_price=101.2,
            optimal_entry=100.0,
            atr_val=2.0,
            max_spread_pct=0.001,
            planned_sl=98.0,
            planned_tp=107.0,
            min_rr=1.5,
            allow_confirmed_reclaim=True,
        )

        self.assertTrue(result["passed"])
        self.assertTrue(result["outside_entry_zone"])
        self.assertTrue(result["confirmed_reclaim_override"])
        self.assertAlmostEqual(result["repriced_rr"], 1.8125)

    def test_confirmed_post_touch_reclaim_still_fails_when_repriced_rr_is_low(self):
        result = main_module.evaluate_executable_entry_price(
            side="buy",
            ticker={"bid": 101.19, "ask": 101.2, "last": 101.2},
            reference_price=101.2,
            optimal_entry=100.0,
            atr_val=2.0,
            max_spread_pct=0.001,
            planned_sl=98.0,
            planned_tp=105.0,
            min_rr=1.5,
            allow_confirmed_reclaim=True,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "EXECUTABLE_RR_BELOW_MINIMUM")
        self.assertLess(result["repriced_rr"], result["min_rr"])

    def test_execution_gate_forwards_verified_post_touch_reclaim(self):
        state, client, risk = self._execution_dependencies(
            None,
            {"status": "CLOSED", "amount": 0, "positions": []},
        )
        client.exchange.fetch_ticker.return_value = {
            "bid": 101.19,
            "ask": 101.2,
            "last": 101.2,
        }
        client.last_execution_failure = {}
        coin = {
            "entry_confirmation": {
                "passed": True,
                "reason": "CONFIRMED",
                "evidence": {
                    "zone_touch_recorded": True,
                    "entry_location_valid": True,
                    "directional_close": True,
                    "reclaim": True,
                },
            },
            "optimal_entry": 100.0,
            "target_plan": {"sl": 98.0, "tp": 107.0},
        }

        with patch.object(main_module, "audit_decision_event") as audit:
            result = main_module.execute_and_protect_trade(
                "BTC/USDT", "buy", 0.1, 101.2, 5, 0.0025, 2.0,
                coin, None, None, {}, "buy", client, risk, Mock(), state,
                Mock(), set(), [], trade_id="confirmed-reclaim",
            )

        self.assertFalse(result)
        client.execute_trade.assert_called_once()
        self.assertTrue(any(
            call.kwargs.get("stage") == "EXECUTABLE_PRICE_GUARD"
            and call.kwargs.get("status") == "PASS"
            and call.kwargs.get("details", {}).get("confirmed_reclaim_override")
            is True
            for call in audit.call_args_list
        ))

    def test_executable_price_reports_repriced_rr_below_minimum(self):
        result = main_module.evaluate_executable_entry_price(
            side="buy",
            ticker={"bid": 0.05879, "ask": 0.05880, "last": 0.05881},
            reference_price=0.05881,
            optimal_entry=0.05867,
            atr_val=0.0004178136904092647,
            max_spread_pct=0.001,
            planned_sl=0.05791793535726333,
            planned_tp=0.060029820000000005,
            min_rr=1.5,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "EXECUTABLE_RR_BELOW_MINIMUM")
        self.assertAlmostEqual(result["repriced_rr"], 1.3942, places=3)

    def test_missing_demo_ticker_quotes_use_futures_order_book_top(self):
        client = Mock()
        client.exchange.fetch_order_book.return_value = {
            "bids": [[99.99, 12.0]],
            "asks": [[100.0, 8.0]],
        }

        ticker = main_module.fetch_executable_contract_ticker(
            client,
            "BTC/USDT",
            ticker={"last": 100.0, "bid": None, "ask": None},
        )

        self.assertEqual(ticker["bid"], 99.99)
        self.assertEqual(ticker["ask"], 100.0)
        self.assertEqual(
            ticker["_executable_price_source"], "FUTURES_ORDER_BOOK_TOP"
        )
        client.exchange.fetch_order_book.assert_called_once_with(
            "BTC/USDT", limit=5
        )
        guard = main_module.evaluate_executable_entry_price(
            side="buy",
            ticker=ticker,
            reference_price=100.0,
            optimal_entry=100.0,
            atr_val=2.0,
        )
        self.assertTrue(guard["passed"])

    def test_missing_ticker_and_order_book_quotes_fail_closed(self):
        client = Mock()
        client.exchange.fetch_order_book.return_value = {"bids": [], "asks": []}

        ticker = main_module.fetch_executable_contract_ticker(
            client,
            "BTC/USDT",
            ticker={"last": 100.0, "bid": None, "ask": None},
        )

        self.assertEqual(ticker["_executable_price_source"], "UNAVAILABLE")
        guard = main_module.evaluate_executable_entry_price(
            side="buy",
            ticker=ticker,
            reference_price=100.0,
            optimal_entry=100.0,
            atr_val=2.0,
        )
        self.assertFalse(guard["passed"])
        self.assertEqual(guard["reason"], "INVALID_EXECUTABLE_PRICE")

    def test_missing_shared_confirmation_cannot_reach_binance(self):
        client = Mock()
        client.has_open_position.return_value = False
        state = Mock()
        state.get.side_effect = lambda key, default=None: (
            False if key == "kill_switch_active" else default
        )
        state.get_order_intent.return_value = {}
        state.get_filter_profile.return_value = "smc_balanced"
        scan_results = []

        with patch.object(main_module, "audit_decision_event"):
            result = main_module.execute_and_protect_trade(
                symbol="BTC/USDT",
                final_decision="buy",
                amount=0.01,
                current_price=100.0,
                trade_leverage=5,
                trade_risk_pct=0.0025,
                atr_val=1.0,
                coin_data={"_scan_id": "s", "_decision_id": "d"},
                sentiment=None,
                bars_df=None,
                ticker=None,
                ta_trend="buy",
                client=client,
                risk_manager=Mock(),
                trailing_manager=Mock(),
                state_manager=state,
                notifier=Mock(),
                active_symbols=set(),
                scan_results=scan_results,
                trade_id="tr-gate",
            )

        self.assertFalse(result)
        client.execute_trade.assert_not_called()
        self.assertIn("تأكيد دخول 5m", scan_results[0]["reason"])

    def test_close_persistence_retry_uses_backoff_and_notification_throttle(self):
        meta = {}

        first_delay = main_module._schedule_close_persistence_retry(meta, now_ts=1000)
        self.assertEqual(first_delay, 60)
        self.assertFalse(main_module._close_persistence_retry_due(meta, now_ts=1059))
        self.assertTrue(main_module._close_persistence_retry_due(meta, now_ts=1060))
        self.assertTrue(
            main_module._close_persistence_failure_notification_due(meta, now_ts=1000)
        )

        meta["close_persistence_last_notification_at"] = 1000
        second_delay = main_module._schedule_close_persistence_retry(meta, now_ts=1060)
        self.assertEqual(second_delay, 120)
        self.assertFalse(
            main_module._close_persistence_failure_notification_due(meta, now_ts=1200)
        )
        self.assertTrue(
            main_module._close_persistence_failure_notification_due(meta, now_ts=4600)
        )

    def test_trade_row_flattens_market_regime_label_before_sqlite(self):
        entry = {
            "trade_id": "regime-close",
            "side": "BUY",
            "entry_time": "2026-08-26 10:00:00",
            "entry_price": 100.0,
            "amount": 1.0,
            "market_regime": {"label": "STRONG_RISK_ON", "known": True},
        }
        exit_details = {
            "exit_time": "2026-08-26 11:00:00",
            "exit_price": 101.0,
            "pnl": 1.0,
            "commission": 0.1,
            "funding": -0.01,
            "funding_status": "COMPLETE",
            "pnl_complete": True,
        }
        captured = {}

        def _capture(row):
            captured.update(row)
            return True

        with patch.object(main_module, "trade_exists", return_value=False), patch.object(
            main_module, "insert_trade", side_effect=_capture
        ), patch("builtins.open", unittest.mock.mock_open()), patch.object(
            main_module.os.path, "exists", return_value=False
        ):
            self.assertTrue(main_module.log_testnet_trade("BTC/USDT", entry, exit_details))

        self.assertEqual(captured["market_regime"], "STRONG_RISK_ON")
        self.assertEqual(captured["commission"], 0.1)
        self.assertEqual(captured["funding"], -0.01)
        self.assertTrue(captured["pnl_complete"])

    def test_touched_order_checks_next_closed_bar_after_price_reclaims_outside_zone(self):
        now = datetime.now(timezone.utc)
        symbol = "BTC/USDT"
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 110.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "max_age_minutes": 20,
            "entry_zone_touched_at_ms": int((now - timedelta(minutes=1)).timestamp() * 1000),
            "confirmation_deadline_ms": int((now + timedelta(minutes=8)).timestamp() * 1000),
            "last_confirmation_attempt_slot": "older-closed-bar",
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
            "filter_profile_snapshot": {},
            "setup_context": {},
            "planned_rr": 1.8,
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.save_virtual_order.return_value = True
        client = Mock()
        client.fetch_ohlcv.return_value = pd.DataFrame()
        confirmation = {
            "passed": False,
            "bar_id": "new-closed-bar",
            "reason": "WAIT",
            "score": 0,
        }

        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 101.2,
            "source": "WS_FRESH",
        }
        with patch.object(
            main_module,
            "ws_manager",
            ws,
            create=True,
        ), patch.object(
            main_module,
            "evaluate_entry_confirmation",
            return_value=confirmation,
        ) as evaluate, patch.object(main_module, "audit_decision_event"):
            main_module.monitor_virtual_orders(
                client,
                state,
                Mock(),
                Mock(),
                Mock(),
            )

        evaluate.assert_called_once()
        self.assertTrue(evaluate.call_args.kwargs["zone_touch_recorded"])

    def test_lagging_confirmation_bar_schedules_bounded_retry_not_slot_skip(self):
        now = datetime.now(timezone.utc)
        symbol = "BTC/USDT"
        order = {
            "side": "buy", "optimal_entry": 100.0, "entry_price": 100.0,
            "sl": 98.0, "tp": 110.0, "invalidation_level": 98.0, "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "max_age_minutes": 20,
            "entry_zone_touched_at_ms": int((now - timedelta(minutes=1)).timestamp() * 1000),
            "confirmation_deadline_ms": int((now + timedelta(minutes=8)).timestamp() * 1000),
            "last_confirmation_attempt_slot": "older-closed-bar",
            "last_price_source": "WS_FRESH", "invalidation_state": "VALID",
            "filter_profile_snapshot": {}, "setup_context": {}, "planned_rr": 1.8,
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.save_virtual_order.return_value = True
        client = Mock()
        client.fetch_ohlcv.return_value = pd.DataFrame()
        confirmation = {
            "passed": False, "bar_id": "stale-bar", "reason": "STALE",
            "closed_bar": {"exact_closed_bar": False},
        }
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 101.2, "source": "WS_FRESH",
        }

        with patch.object(main_module, "ws_manager", ws, create=True), patch.object(
            main_module, "evaluate_entry_confirmation", return_value=confirmation,
        ), patch.object(main_module, "audit_decision_event"):
            main_module.monitor_virtual_orders(
                client, state, Mock(), Mock(), Mock(),
            )

        saved = state.save_virtual_order.call_args_list[-1].args[1]
        self.assertEqual(saved["last_confirmation_attempt_slot"], "older-closed-bar")
        self.assertEqual(saved["confirmation_data_retry_count"], 1)
        self.assertGreater(saved["next_confirmation_data_retry_ms"], int(now.timestamp() * 1000))

    def test_untouched_order_outside_zone_does_not_poll_confirmation_candles(self):
        now = datetime.now(timezone.utc)
        symbol = "BTC/USDT"
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 110.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "created_at": (now - timedelta(minutes=5)).isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "max_age_minutes": 20,
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        client = Mock()

        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 101.2,
            "source": "WS_FRESH",
        }
        with patch.object(
            main_module,
            "ws_manager",
            ws,
            create=True,
        ), patch.object(main_module, "evaluate_entry_confirmation") as evaluate:
            main_module.monitor_virtual_orders(
                client,
                state,
                Mock(),
                Mock(),
                Mock(),
            )

        evaluate.assert_not_called()
        client.fetch_ohlcv.assert_not_called()

    def test_misaligned_contract_price_cannot_start_entry_confirmation(self):
        now = datetime.now(timezone.utc)
        symbol = "JCT/USDT"
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 110.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=2)).timestamp() * 1000),
            "max_age_minutes": 20,
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        client = Mock()
        client.exchange.fetch_ticker.return_value = {
            "last": 102.0, "bid": 101.9, "ask": 102.0,
        }
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 100.0, "source": "WS_FRESH",
        }

        with patch.object(main_module, "ws_manager", ws, create=True), patch.object(
            main_module, "audit_decision_event"
        ) as audit, patch.object(main_module, "evaluate_entry_confirmation") as evaluate:
            main_module.monitor_virtual_orders(
                client, state, Mock(), Mock(), Mock()
            )

        self.assertNotIn("entry_zone_touched_at_ms", order)
        evaluate.assert_not_called()
        client.fetch_ohlcv.assert_not_called()
        self.assertTrue(any(
            call.kwargs.get("stage") == "ENTRY_PRICE_BASIS"
            and call.kwargs.get("status") == "EXECUTABLE_GUARD_REJECTED"
            for call in audit.call_args_list
        ))

    def test_demo_ticker_without_quotes_uses_order_book_and_records_touch(self):
        now = datetime.now(timezone.utc)
        interval_ms = 5 * 60 * 1000
        expected_closed_bar = str(
            (int(now.timestamp() * 1000) // interval_ms - 1) * interval_ms
        )
        symbol = "JCT/USDT"
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 110.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=2)).timestamp() * 1000),
            "max_age_minutes": 20,
            "last_confirmation_bar": expected_closed_bar,
            "last_confirmation_attempt_slot": expected_closed_bar,
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.save_virtual_order.return_value = True
        client = Mock()
        client.exchange.fetch_ticker.return_value = {
            "last": 100.0, "bid": None, "ask": None,
        }
        client.exchange.fetch_order_book.return_value = {
            "bids": [[99.99, 100.0]], "asks": [[100.0, 100.0]],
        }
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 100.0, "source": "WS_FRESH",
        }

        with patch.object(main_module, "ws_manager", ws, create=True), patch.object(
            main_module, "audit_decision_event"
        ) as audit, patch.object(
            main_module, "check_virtual_order_cancellation", return_value=None
        ), patch.object(main_module, "evaluate_entry_confirmation") as evaluate:
            main_module.monitor_virtual_orders(
                client, state, Mock(), Mock(), Mock()
            )

        self.assertIn("entry_zone_touched_at_ms", order)
        self.assertEqual(
            order["entry_zone_touch_price_source"], "FUTURES_ORDER_BOOK_TOP"
        )
        self.assertGreater(order["confirmation_deadline_ms"], int(now.timestamp() * 1000))
        evaluate.assert_not_called()
        self.assertTrue(any(
            call.kwargs.get("stage") == "ENTRY_ZONE_TOUCH"
            and call.kwargs.get("status") == "TOUCHED"
            for call in audit.call_args_list
        ))

    def test_touch_evidence_survives_fresh_context_revalidation_and_rr_stays_closed(self):
        now = datetime.now(timezone.utc)
        symbol = "BTC/USDT"
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 110.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "max_age_minutes": 20,
            "entry_zone_touched_at_ms": int((now - timedelta(minutes=1)).timestamp() * 1000),
            "confirmation_deadline_ms": int((now + timedelta(minutes=8)).timestamp() * 1000),
            "last_confirmation_attempt_slot": "older-closed-bar",
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
            "filter_profile_snapshot": {"min_rr": 1.5},
            "setup_context": {},
            "planned_rr": 1.8,
            "market_levels": {},
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.get_max_open_trades.return_value = 5
        state.save_virtual_order.return_value = True

        bars_15m = pd.DataFrame({
            "timestamp": pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=212, freq="15min"),
            "open": [100.0] * 212,
            "high": [101.0] * 212,
            "low": [99.0] * 212,
            "close": [100.0] * 212,
            "volume": [1000.0] * 212,
            "ema_50": [101.0] * 212,
            "ema_200": [99.0] * 212,
            "adx": [25.0] * 212,
            "atr": [2.0] * 212,
        })
        client = Mock()
        client.fetch_ohlcv.side_effect = [pd.DataFrame(), bars_15m]
        ta_engine = Mock()
        ta_engine.add_indicators.return_value = bars_15m
        ta_engine.get_htf_trend.return_value = "bullish"
        risk = Mock()
        risk.calculate_sl_tp.return_value = (98.0, None)
        risk.last_block_reason = "RR below profile minimum"
        passed = {
            "passed": True,
            "bar_id": "new-closed-bar",
            "reason": "CONFIRMED",
            "score": 80,
        }
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 101.2,
            "source": "WS_FRESH",
        }

        with patch.object(
            main_module,
            "ws_manager",
            ws,
            create=True,
        ), patch.object(
            main_module,
            "evaluate_entry_confirmation",
            side_effect=[passed, passed],
        ) as evaluate, patch.object(
            main_module,
            "detect_market_regime",
            return_value={"label": "MILD_RISK_ON"},
        ), patch.object(
            main_module,
            "finalize_virtual_order",
        ) as finalize, patch.object(
            main_module,
            "execute_and_protect_trade",
        ) as execute, patch.object(main_module, "audit_decision_event"):
            main_module.monitor_virtual_orders(
                client,
                state,
                Mock(),
                Mock(),
                risk,
                ta_engine=ta_engine,
            )

        self.assertEqual(evaluate.call_count, 2)
        self.assertTrue(evaluate.call_args_list[0].kwargs["zone_touch_recorded"])
        self.assertTrue(evaluate.call_args_list[1].kwargs["zone_touch_recorded"])
        finalize.assert_called_once()
        self.assertIn("RR_INVALID_AT_TRIGGER", finalize.call_args.args[4])
        execute.assert_not_called()

    def test_touched_reclaim_forwards_fresh_repriced_plan_to_execution_gate(self):
        now = datetime.now(timezone.utc)
        symbol = "BTC/USDT"
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 110.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "max_age_minutes": 20,
            "entry_zone_touched_at_ms": int((now - timedelta(minutes=1)).timestamp() * 1000),
            "confirmation_deadline_ms": int((now + timedelta(minutes=8)).timestamp() * 1000),
            "last_confirmation_attempt_slot": "older-closed-bar",
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
            "filter_profile_snapshot": {"min_rr": 1.5},
            "setup_context": {},
            "planned_rr": 1.8,
            "market_levels": {},
            "amount": 0.1,
            "trade_risk_pct": 0.0025,
            "trade_leverage": 5,
            "support": 98.0,
            "resistance": 107.0,
            "sentiment_label": "neutral",
            "sentiment_score": 0.0,
            "ta_trend": "buy",
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.get_max_open_trades.return_value = 5
        state.save_virtual_order.return_value = True

        bars_15m = pd.DataFrame({
            "timestamp": pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=212, freq="15min"),
            "open": [100.0] * 212,
            "high": [101.0] * 212,
            "low": [99.0] * 212,
            "close": [100.0] * 212,
            "volume": [1000.0] * 212,
            "ema_50": [101.0] * 212,
            "ema_200": [99.0] * 212,
            "adx": [25.0] * 212,
            "atr": [2.0] * 212,
        })
        client = Mock()
        client.fetch_ohlcv.side_effect = [pd.DataFrame(), bars_15m]
        client.get_balance.return_value = {"USDT": {"total": 1000.0, "free": 1000.0}}
        client.exchange.fetch_ticker.return_value = {
            "bid": 101.19, "ask": 101.2, "last": 101.2,
        }
        ta_engine = Mock()
        ta_engine.add_indicators.return_value = bars_15m
        ta_engine.get_htf_trend.return_value = "bullish"
        risk = Mock()
        risk.calculate_sl_tp.return_value = (98.0, 107.0)
        risk.calculate_position_size.return_value = 0.08
        risk.last_plan = {"valid": True, "sl": 98.0, "tp": 107.0, "rr": 1.81}
        risk.last_block_reason = None
        passed = {
            "passed": True,
            "bar_id": "new-closed-bar",
            "reason": "CONFIRMED",
            "score": 80,
            "evidence": {
                "zone_touch_recorded": True,
                "entry_location_valid": True,
                "directional_close": True,
                "reclaim": True,
            },
        }
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 101.2,
            "source": "WS_FRESH",
        }

        with patch.object(
            main_module, "ws_manager", ws, create=True,
        ), patch.object(
            main_module, "evaluate_entry_confirmation", side_effect=[passed, passed],
        ), patch.object(
            main_module, "detect_market_regime", return_value={"label": "MILD_RISK_ON"},
        ), patch.object(
            main_module, "execute_and_protect_trade", return_value=True,
        ) as execute, patch.object(main_module, "audit_decision_event"):
            main_module.monitor_virtual_orders(
                client, state, Mock(), Mock(), risk, ta_engine=ta_engine,
            )

        execute.assert_called_once()
        forwarded = execute.call_args.kwargs["coin_data"]
        self.assertEqual(forwarded["target_plan"], risk.last_plan)
        self.assertEqual(forwarded["entry_confirmation"], passed)
        self.assertEqual(forwarded["optimal_entry"], 100.0)
        self.assertEqual(execute.call_args.kwargs["current_price"], 101.2)
        state.remove_virtual_order.assert_called_once_with(symbol)

    def test_touched_order_does_not_recheck_the_same_closed_bar(self):
        now = datetime.now(timezone.utc)
        interval_ms = 5 * 60 * 1000
        expected_closed_bar = (
            (int(now.timestamp() * 1000) // interval_ms) - 1
        ) * interval_ms
        symbol = "BTC/USDT"
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 110.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "max_age_minutes": 20,
            "entry_zone_touched_at_ms": int((now - timedelta(minutes=1)).timestamp() * 1000),
            "confirmation_deadline_ms": int((now + timedelta(minutes=8)).timestamp() * 1000),
            "last_confirmation_bar": str(expected_closed_bar),
            "last_confirmation_attempt_slot": expected_closed_bar,
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        client = Mock()
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 101.2,
            "source": "WS_FRESH",
        }

        with patch.object(
            main_module,
            "ws_manager",
            ws,
            create=True,
        ), patch.object(main_module, "evaluate_entry_confirmation") as evaluate:
            main_module.monitor_virtual_orders(
                client,
                state,
                Mock(),
                Mock(),
                Mock(),
            )

        evaluate.assert_not_called()
        client.fetch_ohlcv.assert_not_called()

    def test_attempt_slot_cannot_suppress_an_unconsumed_closed_bar(self):
        now = datetime.now(timezone.utc)
        interval_ms = 5 * 60 * 1000
        expected_closed_bar = str(
            (int(now.timestamp() * 1000) // interval_ms - 1) * interval_ms
        )
        symbol = "XVG/USDT"
        order = {
            "side": "sell",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 102.0,
            "tp": 96.0,
            "invalidation_level": 102.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=8)).timestamp() * 1000),
            "max_age_minutes": 20,
            "entry_zone_touched_at_ms": int((now - timedelta(minutes=6)).timestamp() * 1000),
            "confirmation_deadline_ms": int((now + timedelta(minutes=5)).timestamp() * 1000),
            # محاكاة حالة الإنتاج: علامة المحاولة تقدمت، لكن bar_id الفعلي
            # ما زال أقدم ويجب ألا يمنع استهلاك الشمعة الجديدة.
            "last_confirmation_bar": str(int(expected_closed_bar) - interval_ms),
            "last_confirmation_attempt_slot": expected_closed_bar,
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
            "filter_profile_snapshot": {},
            "setup_context": {},
            "planned_rr": 2.0,
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.save_virtual_order.return_value = True
        client = Mock()
        client.fetch_ohlcv.return_value = pd.DataFrame()
        confirmation = {
            "passed": False,
            # Some exchange adapters omit the timestamp even though the
            # returned row is the exact closed slot. The monitor must use the
            # expected slot as a bounded causal fallback rather than polling
            # the same candle on every 2-second tick.
            "bar_id": None,
            "reason": "WAIT",
            "score": 0,
            "closed_bar": {"exact_closed_bar": True},
        }
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 98.5,
            "source": "WS_FRESH",
        }

        with patch.object(main_module, "ws_manager", ws, create=True), patch.object(
            main_module, "evaluate_entry_confirmation", return_value=confirmation,
        ) as evaluate, patch.object(main_module, "audit_decision_event"):
            main_module.monitor_virtual_orders(
                client, state, Mock(), Mock(), Mock()
            )

        evaluate.assert_called_once()
        self.assertEqual(order["last_confirmation_bar"], expected_closed_bar)

    def test_virtual_guard_does_not_poll_candles_on_every_monitor_tick(self):
        client = Mock()
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "invalidation_level": 98.0,
        }

        reason = main_module.check_virtual_order_cancellation(
            client,
            "BTC/USDT",
            order,
            current_price=100.1,
            check_structure=False,
        )

        self.assertIsNone(reason)
        client.fetch_ohlcv.assert_not_called()

    def test_mark_only_basis_backoff_skips_recheck_but_keeps_safety_guards(self):
        now = datetime.now(timezone.utc)
        symbol = "BTC/USDT"
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 110.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=2)).timestamp() * 1000),
            "max_age_minutes": 20,
            "expires_at": (now + timedelta(minutes=18)).isoformat(),
            "entry_basis_retry_until_ms": int(
                (now + timedelta(seconds=30)).timestamp() * 1000
            ),
            "last_price_source": "WS_FRESH",
            "invalidation_state": "VALID",
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.save_virtual_order.return_value = True
        client = Mock()
        ws = Mock()
        ws.get_price_with_source.return_value = {"price": 100.0, "source": "WS_FRESH"}

        with patch.object(main_module, "ws_manager", ws, create=True), patch.object(
            main_module, "evaluate_entry_confirmation"
        ) as evaluate:
            main_module.monitor_virtual_orders(client, state, Mock(), Mock(), Mock())

        # The timer suppresses the expensive order-book/5m path only;
        # cancellation guards above it still ran and no order was submitted.
        evaluate.assert_not_called()
        self.assertFalse(any(
            call.kwargs.get("timeframe") == "5m"
            for call in client.fetch_ohlcv.call_args_list
        ))
        client.exchange.fetch_ticker.assert_not_called()

    def test_virtual_guard_treats_live_invalidation_as_pending_sweep(self):
        client = Mock()
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "invalidation_level": 98.0,
        }

        reason = main_module.check_virtual_order_cancellation(
            client,
            "BTC/USDT",
            order,
            current_price=98.0,
            check_structure=False,
        )

        self.assertIsNone(reason)
        self.assertEqual(order["invalidation_state"], "SWEEP_PENDING")
        client.fetch_ohlcv.assert_not_called()

    def test_entry_touch_confirmation_window_supersedes_setup_expiry(self):
        client = Mock()
        now = datetime.now(timezone.utc)
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "invalidation_level": 98.0,
            "expires_at": (now - timedelta(minutes=1)).isoformat(),
            "confirmation_deadline_ms": int(
                (now + timedelta(minutes=8)).timestamp() * 1000
            ),
        }

        reason = main_module.check_virtual_order_cancellation(
            client,
            "BTC/USDT",
            order,
            current_price=100.0,
            check_structure=False,
        )

        self.assertIsNone(reason)

    def test_virtual_guard_compares_aware_expiry_in_utc(self):
        client = Mock()
        now = datetime.now(timezone.utc)
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "invalidation_level": 98.0,
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
        }

        reason = main_module.check_virtual_order_cancellation(
            client,
            "BTC/USDT",
            order,
            current_price=100.0,
            check_structure=False,
        )

        self.assertIsNone(reason)

    def test_virtual_guard_interprets_legacy_naive_expiry_as_utc(self):
        client = Mock()
        expired = datetime.now(timezone.utc) - timedelta(minutes=5)
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "invalidation_level": 98.0,
            "expires_at": expired.strftime("%Y-%m-%d %H:%M:%S"),
        }

        reason = main_module.check_virtual_order_cancellation(
            client,
            "BTC/USDT",
            order,
            current_price=100.0,
            check_structure=False,
        )

        self.assertIn("انتهت صلاحية اكتشاف الإعداد", reason)

    def test_virtual_guard_requires_closed_15m_for_hard_invalidation(self):
        timestamps = pd.date_range(
            end=pd.Timestamp.now(tz="UTC"), periods=212, freq="15min"
        )
        bars = pd.DataFrame({
            "timestamp": timestamps,
            "open": [100.0] * 212,
            "high": [101.0] * 212,
            "low": [99.0] * 212,
            "close": [100.0] * 212,
            "volume": [1000.0] * 212,
        })
        bars.loc[bars.index[-2], "close"] = 97.8
        client = Mock()
        client.fetch_ohlcv.return_value = bars
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "invalidation_level": 98.0,
            "invalidation_state": "SWEEP_PENDING",
            "soft_invalidation_at": (
                datetime.now(timezone.utc) - timedelta(minutes=20)
            ).isoformat(),
        }

        reason = main_module.check_virtual_order_cancellation(
            client,
            "BTC/USDT",
            order,
            current_price=97.9,
            check_structure=True,
        )

        self.assertIn("HARD_STRUCTURAL_INVALIDATION", reason)
        self.assertEqual(order["invalidation_state"], "HARD_INVALIDATED")

    def test_reclaim_refreshes_5m_confirmation_window(self):
        timestamps = pd.date_range(
            end=pd.Timestamp.now(tz="UTC"), periods=212, freq="15min"
        )
        bars = pd.DataFrame({
            "timestamp": timestamps,
            "open": [100.0] * 212,
            "high": [101.0] * 212,
            "low": [97.5] * 212,
            "close": [100.0] * 212,
            "volume": [1000.0] * 212,
        })
        bars.loc[bars.index[-2], "close"] = 98.4
        client = Mock()
        client.fetch_ohlcv.return_value = bars
        now = datetime.now(timezone.utc)
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "invalidation_level": 98.0,
            "invalidation_state": "SWEEP_PENDING",
            "soft_invalidation_at": (now - timedelta(minutes=20)).isoformat(),
            "structural_resolution_deadline_ms": int(
                (now + timedelta(minutes=1)).timestamp() * 1000
            ),
        }

        with patch.object(Config, "VIRTUAL_ORDER_CANCEL_IF_TREND_FLIPS", False):
            reason = main_module.check_virtual_order_cancellation(
                client,
                "BTC/USDT",
                order,
                current_price=98.4,
                check_structure=True,
            )

        self.assertIsNone(reason)
        self.assertEqual(order["invalidation_state"], "RECLAIM_CONFIRMED")
        self.assertIsInstance(
            order["structural_close_evidence"]["open_time"], str
        )
        json.dumps(order)
        self.assertNotIn("structural_resolution_deadline_ms", order)
        self.assertGreater(
            order["confirmation_deadline_ms"], int(now.timestamp() * 1000)
        )

    def test_sweep_retries_when_rest_returns_pre_breach_closed_bar(self):
        now = datetime.now(timezone.utc)
        symbol = "BTC/USDT"

        def _bars(last_closed_open, last_closed_price):
            timestamps = pd.date_range(
                end=pd.Timestamp(last_closed_open) + pd.Timedelta(minutes=15),
                periods=212,
                freq="15min",
            )
            frame = pd.DataFrame({
                "timestamp": timestamps,
                "open": [100.0] * 212,
                "high": [101.0] * 212,
                "low": [97.5] * 212,
                "close": [100.0] * 212,
                "volume": [1000.0] * 212,
            })
            frame.loc[frame.index[-2], "timestamp"] = pd.Timestamp(
                last_closed_open
            )
            frame.loc[frame.index[-2], "close"] = last_closed_price
            return frame

        stale_bars = _bars(now - timedelta(minutes=30), 100.0)
        post_breach_bars = _bars(now - timedelta(minutes=15), 97.8)
        order = {
            "side": "buy",
            "optimal_entry": 100.0,
            "entry_price": 100.0,
            "sl": 98.0,
            "tp": 103.0,
            "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "max_age_minutes": 20,
            "entry_zone_touched_at_ms": int(
                (now - timedelta(minutes=1)).timestamp() * 1000
            ),
            "confirmation_deadline_ms": int(
                (now + timedelta(minutes=8)).timestamp() * 1000
            ),
            "structural_resolution_deadline_ms": int(
                (now + timedelta(minutes=5)).timestamp() * 1000
            ),
            "soft_invalidation_at": (now - timedelta(minutes=1)).isoformat(),
            "invalidation_state": "SWEEP_PENDING",
            "last_price_source": "WS_FRESH",
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.save_virtual_order.return_value = True
        client = Mock()
        client.fetch_ohlcv.side_effect = [stale_bars, post_breach_bars]
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 97.9,
            "source": "WS_FRESH",
        }

        with patch.object(
            main_module, "ws_manager", ws, create=True
        ), patch.object(
            main_module, "finalize_virtual_order"
        ) as finalize, patch.object(main_module, "audit_decision_event"):
            main_module.monitor_virtual_orders(
                client, state, Mock(), Mock(), Mock()
            )

            self.assertEqual(client.fetch_ohlcv.call_count, 1)
            self.assertFalse(
                order["structural_close_evidence"]["closed_after_breach"]
            )
            self.assertGreater(
                order["next_structure_check_ms"], int(now.timestamp() * 1000)
            )
            self.assertNotIn("last_structure_guard_bar", order)
            finalize.assert_not_called()

            # A monitor tick before the bounded retry must not hammer REST.
            main_module.monitor_virtual_orders(
                client, state, Mock(), Mock(), Mock()
            )
            self.assertEqual(client.fetch_ohlcv.call_count, 1)

            # Once the retry is due, the first post-breach close is consumed.
            order["next_structure_check_ms"] = 0
            main_module.monitor_virtual_orders(
                client, state, Mock(), Mock(), Mock()
            )

        self.assertEqual(client.fetch_ohlcv.call_count, 2)
        self.assertEqual(order["invalidation_state"], "HARD_INVALIDATED")
        finalize.assert_called_once()
        self.assertIn("HARD_STRUCTURAL_INVALIDATION", finalize.call_args.args[4])

    def test_sweep_retry_budget_exhausts_fail_closed(self):
        now = datetime.now(timezone.utc)
        symbol = "BTC/USDT"
        timestamps = pd.date_range(
            end=pd.Timestamp(now - timedelta(minutes=15)), periods=212, freq="15min"
        )
        stale_bars = pd.DataFrame({
            "timestamp": timestamps,
            "open": [100.0] * 212,
            "high": [101.0] * 212,
            "low": [97.5] * 212,
            "close": [100.0] * 212,
            "volume": [1000.0] * 212,
        })
        max_attempts = int(Config.VIRTUAL_ORDER_STRUCTURE_MAX_ATTEMPTS)
        order = {
            "side": "buy", "optimal_entry": 100.0, "entry_price": 100.0,
            "sl": 98.0, "tp": 103.0, "invalidation_level": 98.0,
            "atr": 2.0,
            "created_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "max_age_minutes": 20,
            "entry_zone_touched_at_ms": int((now - timedelta(minutes=2)).timestamp() * 1000),
            "confirmation_deadline_ms": int((now + timedelta(minutes=5)).timestamp() * 1000),
            "structural_resolution_deadline_ms": int((now + timedelta(minutes=5)).timestamp() * 1000),
            "structure_expected_close_ms": int((now - timedelta(seconds=30)).timestamp() * 1000),
            "structure_retry_count": max_attempts - 1,
            "next_structure_check_ms": 0,
            "soft_invalidation_at": (now - timedelta(minutes=1)).isoformat(),
            "invalidation_state": "SWEEP_PENDING",
            "last_price_source": "WS_FRESH",
        }
        state = Mock()
        state.get_virtual_orders.return_value = {symbol: order}
        state.get_active_symbols.return_value = set()
        state.save_virtual_order.return_value = True
        client = Mock()
        client.fetch_ohlcv.return_value = stale_bars
        ws = Mock()
        ws.get_price_with_source.return_value = {
            "price": 97.9, "source": "WS_FRESH"
        }

        with patch.object(main_module, "ws_manager", ws, create=True), patch.object(
            main_module, "finalize_virtual_order"
        ) as finalize, patch.object(main_module, "audit_decision_event"):
            main_module.monitor_virtual_orders(client, state, Mock(), Mock(), Mock())

        self.assertEqual(client.fetch_ohlcv.call_count, 1)
        finalize.assert_called_once()
        self.assertIn("STRUCTURAL_EVIDENCE_UNAVAILABLE", finalize.call_args.args[4])


class TestProfileRiskRules(unittest.TestCase):
    def test_strict_profile_rejects_missing_smc_target(self):
        manager = RiskManager()
        manager.calculate_trade_plan = Mock(return_value={"valid": False})

        _sl, tp = manager.calculate_sl_tp(
            "buy", 100.0, atr=1.0, market_levels={"resistances": [110.0]},
            filter_profile={"min_rr": 2.0, "allow_rr_fallback": False},
        )

        self.assertIsNone(tp)
        self.assertIn("2.00R", manager.last_block_reason)
        self.assertEqual(manager.calculate_trade_plan.call_args.kwargs["min_rr"], 2.0)

    def test_flexible_profile_uses_calculated_target_when_smc_target_missing(self):
        manager = RiskManager()
        manager.calculate_trade_plan = Mock(return_value={"valid": False})

        sl, tp = manager.calculate_sl_tp(
            "buy", 100.0, atr=1.0, market_levels={"resistances": [110.0]},
            filter_profile={
                "min_rr": 1.2, "allow_rr_fallback": True, "fallback_rr": 1.2,
            },
        )

        self.assertIsNotNone(tp)
        self.assertGreater(tp, 100.0)
        self.assertLess(sl, 100.0)


class TestSentimentSemantics(unittest.TestCase):
    def test_sentiment_direction_comes_from_sentiment_not_galaxy_score(self):
        engine = SocialEngine()
        response = Mock(status_code=200)
        response.json.return_value = {"data": {
            "galaxy_score": 90,
            "sentiment": 20,
            "social_volume": 1000,
            "social_dominance": 1.0,
            "alt_rank": 10,
        }}
        response.raise_for_status.return_value = None

        with patch("engines.social_engine.requests.get", return_value=response):
            result = engine._try_key("key", "BTC", time.time())

        self.assertEqual(result["label"], "negative")
        self.assertEqual(result["raw_sentiment_pct"], 20.0)
        self.assertEqual(result["galaxy_score"], 90)

    def test_placeholder_sentiment_with_zero_volume_is_rejected(self):
        engine = SocialEngine()
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"data": {
            "galaxy_score": 50,
            "sentiment": "50",
            "social_volume": "0",
        }}
        response.raise_for_status.return_value = None

        with patch("engines.social_engine.requests.get", return_value=response):
            result = engine._try_key("secret-key", "ZEC", time.time())

        self.assertIsNone(result)

    def test_402_cools_down_only_the_key_symbol_pair(self):
        engine = SocialEngine()
        response = Mock(status_code=402, headers={})
        response.raise_for_status.return_value = None

        with patch("engines.social_engine.requests.get", return_value=response):
            self.assertIsNone(engine._try_key("secret-key", "PEPE", time.time()))

        self.assertIn(("secret-key", "PEPE"), engine._symbol_cooldowns)
        self.assertNotIn("secret-key", engine._key_cooldowns)

    def test_multiplier_contract_is_normalized_for_social_api(self):
        self.assertEqual(SocialEngine._normalize_social_symbol("1000PEPE/USDT:USDT"), "PEPE")


class TestWebAppAuth(unittest.TestCase):
    @staticmethod
    def _signed_data(token, user_id, auth_date):
        values = {
            "auth_date": str(auth_date),
            "query_id": "query-1",
            "user": json.dumps({"id": user_id}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(values)

    def test_valid_data_is_limited_to_expected_user_and_age(self):
        token = "test-token"
        now = int(time.time())
        init_data = self._signed_data(token, 123, now)

        self.assertTrue(_verify_telegram_init_data(init_data, token, expected_user_id=123))
        self.assertFalse(_verify_telegram_init_data(init_data, token, expected_user_id=999))

        expired = self._signed_data(token, 123, now - 7200)
        self.assertFalse(_verify_telegram_init_data(expired, token, expected_user_id=123, max_age_seconds=3600))

    def test_dashboard_fails_closed_without_explicit_webapp_user_id(self):
        from webapp.server import app

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False):
            with patch.dict(os.environ, {"TELEGRAM_WEBAPP_USER_ID": ""}, clear=False):
                response = app.test_client().get("/api/dashboard")

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
