"""
Unit Tests — DerivativesRiskFilter
يختبر: أن الفلتر لا يعيد بيانات عشوائية، وأن هيكل الإخراج صحيح
يُحاكي استدعاءات API باستخدام unittest.mock
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDerivativesRiskFilter(unittest.TestCase):

    def _make_filter(self):
        from filters.derivatives_risk_filter import DerivativesRiskFilter
        return DerivativesRiskFilter(state_manager=None)

    # ── 1. هيكل النتيجة دائماً صحيح ──────────────────────────────
    @patch("filters.derivatives_risk_filter.requests.get")
    def test_result_structure(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": {"longShortRatio": "1.2", "longAccount": "0.55"}}
        )
        f = self._make_filter()
        result = f.evaluate_risk("BTCUSDT", "BUY", price=40000.0)
        self.assertIn("decision", result, "يجب وجود مفتاح decision")
        self.assertIn(result["decision"], ["ALLOW", "REJECT", "WARN"],
                      "decision يجب أن يكون ALLOW أو REJECT أو WARN")

    # ── 2. فشل API → ALLOW آمن ───────────────────────────────────
    @patch("filters.derivatives_risk_filter.requests.get", side_effect=Exception("timeout"))
    def test_api_failure_returns_allow(self, _):
        f = self._make_filter()
        result = f.evaluate_risk("BTCUSDT", "BUY", price=40000.0)
        self.assertEqual(result["decision"], "ALLOW",
                         "عند فشل API يجب أن يعود ALLOW وليس رفضاً عشوائياً")

    # ── 3. لا random في النتيجة ───────────────────────────────────
    @patch("filters.derivatives_risk_filter.requests.get", side_effect=Exception("timeout"))
    def test_no_random_on_failure(self, _):
        f = self._make_filter()
        results = {f.evaluate_risk("BTCUSDT", "BUY", price=40000.0)["decision"] for _ in range(20)}
        self.assertEqual(results, {"ALLOW"},
                         "يجب أن تكون جميع النتائج ALLOW عند فشل API — لا عشوائية")

    # ── 4. يعمل مع BUY وSELL ──────────────────────────────────────
    @patch("filters.derivatives_risk_filter.requests.get", side_effect=Exception("timeout"))
    def test_works_with_buy_and_sell(self, _):
        f = self._make_filter()
        for side in ["BUY", "SELL", "LONG", "SHORT"]:
            result = f.evaluate_risk("ETHUSDT", side, price=2000.0)
            self.assertIn("decision", result)

    def test_oi_uses_mainnet_value_and_first_read_has_no_fake_change(self):
        class State:
            def __init__(self):
                self.values = {}
            def get(self, key, default=None):
                return self.values.get(key, default)
            def set(self, key, value):
                self.values[key] = value
                return True

        responses = []
        for payload in (
            {"lastFundingRate": "0.0001"},
            {"openInterest": "12345.5"},
            [{"longShortRatio": "1.2"}],
        ):
            response = MagicMock(status_code=200)
            response.json.return_value = payload
            responses.append(response)

        from filters.derivatives_risk_filter import DerivativesRiskFilter
        state = State()
        with patch("filters.derivatives_risk_filter.requests.get", side_effect=responses):
            result = DerivativesRiskFilter(state_manager=state).evaluate_risk(
                "BTC/USDT:USDT", "BUY", 40000.0
            )

        self.assertEqual(result["open_interest"], 12345.5)
        self.assertIsNone(result["oi_change_pct"])
        self.assertEqual(result["data_status"]["oi_change"], "NO_4_SLOT_BASELINE")

    def test_oi_change_uses_configured_four_slot_baseline(self):
        from datetime import datetime, timezone
        from filters.derivatives_risk_filter import DerivativesRiskFilter
        from core.config import Config

        class State:
            def __init__(self):
                self.values = {}
            def get(self, key, default=None):
                return self.values.get(key, default)
            def set(self, key, value):
                self.values[key] = value
                return True

        interval_ms = 15 * 60 * 1000
        current_slot = (
            int(datetime.now(timezone.utc).timestamp() * 1000) // interval_ms
        ) * interval_ms
        state = State()
        state.values["oi_mainnet_samples_BTCUSDT"] = [{
            "slot_ms": current_slot - int(Config.OI_CHANGE_LOOKBACK) * interval_ms,
            "open_interest": 100.0,
        }]
        risk_filter = DerivativesRiskFilter(state_manager=state)

        change = risk_filter._calculate_oi_change("BTCUSDT", 110.0)

        self.assertAlmostEqual(change, 0.10)
        self.assertEqual(risk_filter._last_oi_change_status, "OK_4_SLOTS")

    @patch("filters.derivatives_risk_filter.requests.get")
    def test_unavailable_oi_is_none_not_zero(self, mock_get):
        funding = MagicMock(status_code=200)
        funding.json.return_value = {"lastFundingRate": "0.0001"}
        missing_oi = MagicMock(status_code=500)
        missing_oi.json.return_value = {}
        ratio = MagicMock(status_code=200)
        ratio.json.return_value = [{"longShortRatio": "1.0"}]
        mock_get.side_effect = [funding, missing_oi, ratio]

        result = self._make_filter().evaluate_risk("BTCUSDT", "BUY", 40000.0)

        self.assertIsNone(result["open_interest"])
        self.assertIsNone(result["oi_change_pct"])
        self.assertEqual(result["data_status"]["open_interest"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
