import unittest
from unittest.mock import Mock, patch

import main as main_module
from core.config import Config
from utils.telegram_bot import TelegramNotifier
from utils.trade_journal import TradeJournal


class TestPostTradeFeedback(unittest.TestCase):
    def test_r_excursions_are_rendered_as_real_percentages(self):
        feedback = TradeJournal().build_post_trade_feedback(
            {
                "symbol": "BTC/USDT",
                "side": "BUY",
                "entry_price": 100.0,
                "initial_sl": 95.0,
                "sl": 95.0,
                "tp": 120.0,
                "leverage": 5,
            },
            {
                "exit_price": 99.0,
                "exit_reason": "STOP_LOSS",
                "pnl_pct": -1.25,
                "mfe_r": 1.5,
                "mae_r": 0.5,
            },
        )

        self.assertTrue(feedback["excursion_available"])
        self.assertAlmostEqual(feedback["max_favorable_pct"], 7.5)
        self.assertAlmostEqual(feedback["max_adverse_pct"], 2.5)
        self.assertIn("+7.50%", feedback["feedback_text"])
        self.assertIn("+2.50%", feedback["feedback_text"])
        self.assertIn("حركة السعر: -1.00%", feedback["feedback_text"])
        self.assertIn("الرافعة: 5x", feedback["feedback_text"])
        self.assertNotIn("-6.2% بالرافعة", feedback["feedback_text"])

    def test_missing_excursion_data_is_not_called_a_sharp_reversal(self):
        feedback = TradeJournal().build_post_trade_feedback(
            {
                "symbol": "BTC/USDT",
                "side": "BUY",
                "entry_price": 100.0,
                "sl": 0.0,
                "tp": 110.0,
            },
            {
                "exit_price": 99.0,
                "exit_reason": "UNKNOWN_CLOSE",
                "pnl_pct": -1.0,
            },
        )

        self.assertEqual(feedback["quality"], "INSUFFICIENT_DATA")
        self.assertFalse(feedback["excursion_available"])
        self.assertIn("غير متاحة", feedback["feedback_text"])
        self.assertNotIn("انعكاس حاد ضدنا (0.00%)", feedback["feedback_text"])
        self.assertNotIn("نقطة الدخول كانت ضعيفة", feedback["feedback_text"])


class TestPnlAlertRendering(unittest.TestCase):
    def _notifier(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.send_message = Mock(return_value=True)
        notifier._safe_html = lambda value: str(value)
        notifier._make_btn = lambda text, data, *args, **kwargs: {
            "text": text,
            "callback_data": data,
        }
        return notifier

    def test_reference_wallet_amount_is_recomputed_from_displayed_percent(self):
        notifier = self._notifier()

        notifier.send_pnl_alert(
            "BNB/USDT",
            "STOP_LOSS",
            749.19,
            -0.92389308,
            wallet_pnl_pct=-0.0183,
            # Deliberately inconsistent legacy value from the duplicate
            # secondary runtime.  The renderer must not display it.
            pnl_ref_50=-0.9239,
            reference_balance=100.0,
        )

        text = notifier.send_message.call_args.args[0]
        self.assertIn("-0.0183 USDT", text)
        self.assertIn("-0.0183%", text)
        normalized_section = text.split("نتيجة معيارية", 1)[1]
        self.assertNotIn("-0.9239 USDT", normalized_section)

    def test_zero_pnl_is_rendered_as_breakeven(self):
        notifier = self._notifier()
        notifier.send_pnl_alert(
            "BTC/USDT", "MANUAL_CLOSE", 100.0, 0.0,
            wallet_pnl_pct=0.0, reference_balance=100.0,
        )
        text = notifier.send_message.call_args.args[0]
        self.assertIn("تعادل", text)
        self.assertNotIn("🔴 خسارة", text)

    def test_outbound_disabled_suppresses_http(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier._persistent_shortcuts = lambda: {}
        with patch.object(Config, "TELEGRAM_OUTBOUND_ENABLED", False), \
             patch("utils.telegram_bot.requests.post") as post:
            self.assertFalse(notifier.send_message("must stay local"))
        post.assert_not_called()


class TestCloseReconciliation(unittest.TestCase):
    def test_zero_position_without_fill_waits_instead_of_fabricating_close(self):
        client = Mock()
        client.exchange.fetch_positions.return_value = []
        client.get_recent_sl_tp_fills.return_value = []
        state = Mock()
        state.get_active_symbols.return_value = {"BTC/USDT"}
        metadata = {
            "symbol": "BTC/USDT",
            "side": "BUY",
            "entry_price": 100.0,
            "amount": 1.0,
            "sl": 95.0,
        }
        state.get_entry_metadata.return_value = metadata

        with patch.object(Config, "ACCOUNT_MANAGEMENT_ENABLED", True), \
             patch("main.time.sleep"), \
             patch("main.handle_trade_close") as close:
            main_module.monitor_active_trades(
                client, state, Mock(), Mock(), Mock(), last_check_time=123,
            )

        close.assert_not_called()
        state.save_entry_metadata.assert_called()
        saved = state.save_entry_metadata.call_args.args[1]
        self.assertEqual(saved["close_fill_retry_count"], 1)
        self.assertIn("close_fill_pending_since", saved)

    def test_passive_secondary_runtime_cannot_touch_account(self):
        client, state = Mock(), Mock()
        with patch.object(Config, "ACCOUNT_MANAGEMENT_ENABLED", False):
            result = main_module.monitor_active_trades(
                client, state, Mock(), Mock(), Mock(), last_check_time=456,
            )
        self.assertEqual(result, 456)
        state.get_active_symbols.assert_not_called()
        client.exchange.fetch_positions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
