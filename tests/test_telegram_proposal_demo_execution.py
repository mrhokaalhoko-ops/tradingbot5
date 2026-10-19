import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.config import Config
from core.exchange import ExchangeClient
import main as main_module
from strategy.risk_manager import RiskManager
from strategy.user_visible_trade_proposal import (
    EVENT_TYPE,
    build_user_visible_trade_proposal,
)
from utils.telegram_proposal_runtime_identity import (
    telegram_proposal_runtime_identity,
)
from utils.telegram_bot import TelegramNotifier


def proposal(proposal_id="proposal-1"):
    return build_user_visible_trade_proposal(
        proposal_id=proposal_id,
        symbol="BTC/USDT",
        side="buy",
        proposed_entry=100.0,
        original_stop=95.0,
        original_target=120.0,
        target_stages=[{"name": "TP1", "price": 107.5, "volume": 0.0}],
        thesis_id="thesis-1",
        setup_id="setup-1",
        confirmation_bar_id="bar-1",
        leverage=5,
        risk_percent=0.25,
        amount=0.1,
        target_plan={"rr": 4.0},
    )


class TestProposalContract(unittest.TestCase):
    def test_polling_disabled_does_not_mutate_shared_telegram_commands_or_menu(self):
        with (
            patch.object(Config, "TELEGRAM_POLLING_ENABLED", False),
            patch("utils.telegram_bot.requests.post") as post,
        ):
            TelegramNotifier()

        post.assert_not_called()

    def test_contract_is_authoritative_and_preserves_original_geometry(self):
        item = proposal()

        self.assertEqual(item["event_type"], EVENT_TYPE)
        self.assertEqual(item["side"], "BUY")
        self.assertEqual(item["proposed_entry"], 100.0)
        self.assertEqual(item["original_stop"], 95.0)
        self.assertEqual(item["original_target"], 120.0)
        self.assertEqual(item["target_stages"][0]["volume"], 0.0)
        self.assertEqual(item["execution_authority"], "DEMO_ONLY")
        self.assertTrue(item["event_hash"])

    def test_contract_rejects_invalid_stop_instead_of_inventing_one(self):
        with self.assertRaises(ValueError):
            build_user_visible_trade_proposal(
                proposal_id="invalid-stop",
                symbol="BTC/USDT",
                side="buy",
                proposed_entry=100.0,
                original_stop=101.0,
                original_target=120.0,
                target_stages=[],
                thesis_id="thesis",
                setup_id="setup",
                confirmation_bar_id="bar",
                leverage=5,
                risk_percent=0.25,
                amount=0.1,
                target_plan={},
            )

    def test_duplicate_visible_proposal_is_suppressed_before_order_path(self):
        item = proposal()
        state = Mock()
        state.get.return_value = {
            item["proposal_id"]: {"status": "VISIBLE", "proposal": item}
        }
        notifier = Mock()

        with patch.object(main_module, "audit_decision_event"):
            published = main_module._publish_authoritative_trade_proposal(
                state_manager=state,
                notifier=notifier,
                proposal=item,
                audit_ids={"symbol": "BTC/USDT", "trade_id": item["proposal_id"]},
            )

        self.assertFalse(published)
        notifier.send_trade_proposal.assert_not_called()
        state.set.assert_not_called()

    def test_visible_proposal_is_persisted_before_and_after_notification(self):
        item = proposal()
        state = Mock()
        state.get.return_value = {}
        state.set.return_value = True
        notifier = Mock()
        notifier.send_trade_proposal.return_value = True

        with patch.object(main_module, "audit_decision_event"):
            published = main_module._publish_authoritative_trade_proposal(
                state_manager=state,
                notifier=notifier,
                proposal=item,
                audit_ids={"symbol": "BTC/USDT", "trade_id": item["proposal_id"]},
            )

        self.assertTrue(published)
        self.assertEqual(state.set.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in state.set.call_args_list],
            [
                "user_visible_trade_proposals",
                "telegram_proposal_demo_summary",
                "user_visible_trade_proposals",
                "telegram_proposal_demo_summary",
            ],
        )
        notifier.send_trade_proposal.assert_called_once_with(item)

    def test_parity_summary_is_derived_from_the_proposal_ledger(self):
        item = proposal()
        summary = main_module._proposal_parity_summary({
            item["proposal_id"]: {
                "proposal": item,
                "visible_at": "2026-10-10T00:00:00Z",
                "entry_post_count": 1,
                "filled_amount": 0.1,
                "actual_entry": 100.0,
                "status": "CLOSED_RECONCILED",
            }
        })

        self.assertEqual(summary["telegram_trade_proposals_count"], 1)
        self.assertEqual(summary["demo_entry_posts_count"], 1)
        self.assertEqual(summary["demo_fills_count"], 1)
        self.assertEqual(summary["demo_protected_count"], 1)
        self.assertEqual(summary["demo_closed_reconciled_count"], 1)
        self.assertEqual(summary["proposal_without_entry_count"], 0)


class TestProposalToDemoExecution(unittest.TestCase):
    @staticmethod
    def dependencies(*, protection=None, existing_proposal=None):
        ledger = dict(existing_proposal or {})
        state = Mock()

        def state_get(key, default=None):
            if key == "user_visible_trade_proposals":
                return dict(ledger)
            return default

        def state_set(key, value):
            if key == "user_visible_trade_proposals":
                ledger.clear()
                ledger.update(value)
            return True

        state.get.side_effect = state_get
        state.set.side_effect = state_set
        state.is_state_recovery_blocked.return_value = False
        state.get_order_intent.return_value = {}
        state.get_max_open_trades.return_value = 5
        state.get_demo_signal_execution_mode.return_value = "strict_proposal"
        state.get_active_symbols.return_value = set()
        state.save_order_intent.return_value = True
        state.transition_order_intent.return_value = True
        state.save_entry_metadata.return_value = True

        client = Mock()
        client.has_open_position.return_value = False
        client.exchange.fetch_ticker.return_value = {
            "bid": 99.99, "ask": 100.0, "last": 100.0,
        }
        client.execute_trade.return_value = {
            "id": "entry-order-1",
            "clientOrderId": "proposal-trade-1",
            "status": "closed",
            "filled": 0.1,
            "amount": 0.1,
            "average": 100.0,
            "info": {"status": "FILLED", "executedQty": "0.1"},
        }
        client.get_position_status.return_value = {
            "status": "OPEN",
            "amount": 0.1,
            "positions": [{"entry_price": 100.0}],
        }
        client.place_sl_tp.return_value = protection or {
            "sl_ok": True,
            "tp_ok": True,
            "sl_order_id": "sl-algo-1",
            "tp_order_id": "tp-algo-1",
        }
        client.get_balance.return_value = {
            "USDT": {"total": 1000.0, "free": 900.0}, "info": {}
        }

        risk = Mock()
        risk.calculate_sl_tp.return_value = (95.0, 120.0)
        risk.get_tp_steps.return_value = [
            {"name": "TP1", "price": 107.5, "volume": 0.0},
            {"name": "TP2", "price": 112.5, "volume": 0.0},
        ]
        risk.last_plan = {"rr": 4.0}
        risk.last_block_reason = ""

        notifier = Mock()
        notifier.send_trade_proposal.return_value = True
        trailing = Mock()
        return ledger, state, client, risk, notifier, trailing

    @staticmethod
    def execute(state, client, risk, notifier, trailing, trade_id="proposal-trade-1"):
        coin = {
            "entry_confirmation": {
                "passed": True, "reason": "PASS", "bar_id": "bar-1"
            },
            "optimal_entry": 100.0,
            "reason": "authoritative current-runtime setup",
            "winning_strategy": "SMC",
            "_thesis_id": "thesis-1",
        }
        with (
            patch.object(Config, "TELEGRAM_PROPOSAL_DEMO_MODE", True),
            patch.object(Config, "DEMO_EXECUTION_ENABLED", True),
            patch.object(main_module, "audit_decision_event"),
        ):
            return main_module.execute_and_protect_trade(
                "BTC/USDT", "buy", 0.1, 100.0, 5, 0.0025, 2.0,
                coin, None, None, {}, "buy", client, risk, trailing, state,
                notifier, set(), [], trade_id=trade_id,
            )

    def test_one_visible_proposal_creates_one_entry_and_both_protection_legs(self):
        ledger, state, client, risk, notifier, trailing = self.dependencies()
        events = []
        notifier.send_trade_proposal.side_effect = (
            lambda _item: events.append("proposal") or True
        )
        client.execute_trade.side_effect = (
            lambda *args, **kwargs: events.append("entry") or {
                "id": "entry-order-1", "clientOrderId": kwargs["client_order_id"],
                "status": "closed", "filled": 0.1, "amount": 0.1,
                "average": 100.0,
                "info": {"status": "FILLED", "executedQty": "0.1"},
            }
        )

        result = self.execute(state, client, risk, notifier, trailing)

        self.assertTrue(result)
        self.assertEqual(events, ["proposal", "entry"])
        client.execute_trade.assert_called_once()
        client.place_sl_tp.assert_called_once()
        notifier.send_trade_proposal.assert_called_once()
        trailing.register_trade.assert_called_once_with(
            symbol="BTC/USDT", side="buy", entry_price=100.0,
            initial_sl=95.0, amount=0.1,
        )
        current = ledger["proposal-trade-1"]
        self.assertEqual(current["status"], "ENTRY_FILLED_AND_PROTECTED")
        self.assertEqual(current["entry_post_count"], 1)

    def test_replayed_proposal_never_reaches_entry_post(self):
        existing = {
            "proposal-trade-1": {
                "status": "VISIBLE", "proposal": proposal("proposal-trade-1")
            }
        }
        _, state, client, risk, notifier, trailing = self.dependencies(
            existing_proposal=existing
        )

        result = self.execute(state, client, risk, notifier, trailing)

        self.assertFalse(result)
        client.execute_trade.assert_not_called()
        client.place_sl_tp.assert_not_called()
        notifier.send_trade_proposal.assert_not_called()

    def test_demo_mode_uses_configured_portfolio_capacity_not_one(self):
        _, state, client, risk, notifier, trailing = self.dependencies()
        state.get_max_open_trades.return_value = 10
        state.get_active_symbols.return_value = {
            f"ALT{i}/USDT" for i in range(9)
        }

        result = self.execute(state, client, risk, notifier, trailing)

        self.assertTrue(result)
        client.execute_trade.assert_called_once()

    def test_hybrid_demo_authority_bypasses_only_late_strategy_gates(self):
        _, state, client, risk, notifier, trailing = self.dependencies()
        state.get_demo_signal_execution_mode.return_value = "hybrid_decision"
        coin = {
            "entry_confirmation": {
                "passed": False,
                "reason": "NOT_REQUIRED_FOR_USER_VISIBLE_HYBRID_PROPOSAL",
            },
            "proposal_execution_mode": "HYBRID_DECISION_DEMO",
            "optimal_entry": 100.0,
            "reason": "authoritative hybrid BUY",
            "winning_strategy": "SMC",
            "_thesis_id": "hybrid-thesis-1",
        }
        rejecting_ta = Mock()
        rejecting_ta.validate_entry_quality.return_value = (False, "strict reject")

        with (
            patch.object(Config, "TELEGRAM_PROPOSAL_DEMO_MODE", True),
            patch.object(Config, "DEMO_EXECUTION_ENABLED", True),
            patch.object(main_module, "audit_decision_event"),
        ):
            result = main_module.execute_and_protect_trade(
                "BTC/USDT", "buy", 0.1, 100.0, 5, 0.0025, 2.0,
                coin, None, object(), {}, "buy", client, risk, trailing,
                state, notifier, set(), [], ta_engine=rejecting_ta,
                trade_id="hybrid-proposal-1",
            )

        self.assertTrue(result)
        rejecting_ta.validate_entry_quality.assert_not_called()
        client.execute_trade.assert_called_once()
        client.place_sl_tp.assert_called_once()

    def test_strict_mode_still_rejects_missing_confirmation(self):
        _, state, client, risk, notifier, trailing = self.dependencies()
        state.get_demo_signal_execution_mode.return_value = "strict_proposal"
        coin = {
            "entry_confirmation": {"passed": False, "reason": "WAIT"},
            "proposal_execution_mode": "HYBRID_DECISION_DEMO",
            "optimal_entry": 100.0,
        }

        with (
            patch.object(Config, "TELEGRAM_PROPOSAL_DEMO_MODE", True),
            patch.object(Config, "DEMO_EXECUTION_ENABLED", True),
            patch.object(main_module, "audit_decision_event"),
        ):
            result = main_module.execute_and_protect_trade(
                "BTC/USDT", "buy", 0.1, 100.0, 5, 0.0025, 2.0,
                coin, None, None, {}, "buy", client, risk, trailing,
                state, notifier, set(), [], trade_id="strict-proposal-1",
            )

        self.assertFalse(result)
        client.execute_trade.assert_not_called()

    def test_demo_startup_reports_ten_position_cap(self):
        with (
            patch.object(Config, "TELEGRAM_PROPOSAL_DEMO_MODE", True),
            patch.object(Config, "USE_TESTNET", True),
            patch.object(Config, "BINANCE_API_KEY", "demo-key"),
            patch.object(Config, "BINANCE_API_SECRET", "demo-secret"),
            patch.object(Config, "LEVERAGE", 5),
            patch.object(Config, "DEMO_EXECUTION_ENABLED", True),
            patch.object(Config, "TESTNET_MAX_OPEN_TRADES", 10),
            patch.object(Config, "HARD_MAX_OPEN_TRADES", 10),
        ):
            mode = main_module.validate_telegram_proposal_demo_mode()

        self.assertEqual(mode["effective_position_cap"], 10)

    def test_algo_protection_receives_the_final_structural_stop(self):
        _, state, client, _risk, notifier, trailing = self.dependencies()
        state.get_demo_signal_execution_mode.return_value = "hybrid_decision"
        client.get_price_tick_size.return_value = 0.1
        client.preview_order_amount.return_value = 0.1
        structural_risk = RiskManager()
        setup = {
            "type": "bullish_ob", "low": 98.0, "high": 99.0,
            "age": 1, "created_at": "2026-10-11T00:00:00Z",
        }
        coin = {
            "entry_confirmation": {"passed": False, "reason": "NOT_REQUIRED"},
            "proposal_execution_mode": "HYBRID_DECISION_DEMO",
            "optimal_entry": 100.0,
            "reason": "authoritative hybrid BUY",
            "winning_strategy": "SMC",
            "_thesis_id": "structural-thesis-1",
            "_ta_context": {"setup_anchor": setup},
            "market_levels": {
                "bullish_obs": [setup],
                "bearish_fvgs": [{
                    "low": 110.0, "high": 111.0, "age": 1,
                    "created_at": "2026-10-11T00:00:00Z",
                }],
            },
        }

        with (
            patch.object(Config, "TELEGRAM_PROPOSAL_DEMO_MODE", True),
            patch.object(Config, "DEMO_EXECUTION_ENABLED", True),
            patch.object(Config, "STRUCTURAL_INITIAL_STOP_V1_ENABLED", True),
            patch.object(main_module, "audit_decision_event"),
        ):
            result = main_module.execute_and_protect_trade(
                "BTC/USDT", "buy", 0.1, 100.0, 5, 0.0025, 2.0,
                coin, None, object(), {}, "buy", client, structural_risk,
                trailing, state, notifier, set(), [],
                invalidation_level=98.0, trade_id="structural-proposal-1",
            )

        self.assertTrue(result)
        client.execute_trade.assert_called_once()
        protection_call = client.place_sl_tp.call_args
        self.assertEqual(protection_call.args[3], 97.4)
        self.assertLess(protection_call.args[3], setup["low"])
        self.assertEqual(
            structural_risk.last_plan["stop_plan"]["policy"],
            "STRUCTURAL_INITIAL_STOP_V1",
        )

    def test_partial_protection_is_not_accepted_and_position_is_closed(self):
        ledger, state, client, risk, notifier, trailing = self.dependencies(
            protection={
                "sl_ok": True, "tp_ok": False,
                "sl_order_id": "sl-algo-1", "tp_order_id": None,
            }
        )
        client.close_position.return_value = True

        result = self.execute(state, client, risk, notifier, trailing)

        self.assertFalse(result)
        client.execute_trade.assert_called_once()
        client.close_position.assert_called_once_with("BTC/USDT", "buy", 0.1)
        trailing.register_trade.assert_not_called()
        self.assertEqual(
            ledger["proposal-trade-1"]["status"],
            "PROTECTION_FAILED_EMERGENCY_CLOSED",
        )


class TestDemoEnvironmentGuard(unittest.TestCase):
    def test_only_demo_fapi_routes_are_accepted(self):
        client = ExchangeClient.__new__(ExchangeClient)
        client.exchange = Mock()
        client.exchange.urls = {
            "api": {
                "fapiPublic": "https://demo-fapi.binance.com/fapi/v1",
                "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
            }
        }

        proof = client.prove_demo_environment()

        self.assertTrue(proof["is_demo"])
        self.assertFalse(proof["mainnet"])
        self.assertEqual(proof["observed_hosts"], ["demo-fapi.binance.com"])

    def test_mainnet_fapi_route_is_rejected(self):
        client = ExchangeClient.__new__(ExchangeClient)
        client.exchange = Mock()
        client.exchange.urls = {
            "api": {"fapiPrivate": "https://fapi.binance.com/fapi/v1"}
        }

        proof = client.prove_demo_environment()

        self.assertFalse(proof["is_demo"])

    def test_runtime_build_identity_is_reproducible_and_contains_core_files(self):
        first = telegram_proposal_runtime_identity()
        second = telegram_proposal_runtime_identity()

        self.assertEqual(first, second)
        self.assertEqual(len(first["runtime_build_hash"]), 64)
        self.assertIn("main.py", first["runtime_source_manifest"])
        self.assertIn("core/version.py", first["runtime_source_manifest"])
        self.assertIn(
            "utils/runtime_log_exporter.py", first["runtime_source_manifest"]
        )
        self.assertIn(
            "strategy/user_visible_trade_proposal.py",
            first["runtime_source_manifest"],
        )


class TestTelegramUiParity(unittest.TestCase):
    def test_demo_execution_mode_does_not_suppress_existing_telegram_ui(self):
        source = Path(main_module.__file__).read_text(encoding="utf-8")

        self.assertNotIn("general scan summary suppressed", source)
        self.assertNotIn(
            "if not Config.TELEGRAM_PROPOSAL_DEMO_MODE:\n"
            "        notifier.send_scan_summary",
            source,
        )
        self.assertNotIn(
            "and not Config.TELEGRAM_PROPOSAL_DEMO_MODE\n"
            "            ):",
            source,
        )

    def test_release_version_is_1_6_18(self):
        from core.version import APP_VERSION

        self.assertEqual(APP_VERSION, "1.6.18")


if __name__ == "__main__":
    unittest.main()
