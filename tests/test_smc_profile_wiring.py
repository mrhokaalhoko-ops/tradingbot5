import unittest

import pandas as pd
from unittest.mock import Mock

from engines.ta_engine import TAEngine


class TestSmcProfileWiring(unittest.TestCase):
    @staticmethod
    def _bars_without_bos_or_sweep():
        rows = []
        start = pd.Timestamp("2026-07-01T00:00:00Z")
        for index in range(80):
            open_price = 100.00
            close_price = 100.05 if index % 2 == 0 else 99.95
            rows.append({
                "timestamp": start + pd.Timedelta(minutes=15 * index),
                "open": open_price,
                "high": 101.00,
                "low": 99.50,
                "close": close_price,
                "volume": 1000.0,
            })

        # شمعة سابقة هابطة، ثم displacement صاعد، لكن إغلاقه لا يكسر
        # prior_swing_high=101 ولا ينفذ sweep أسفل prior low=99.5.
        rows[29].update({"open": 100.0, "high": 100.5, "low": 99.5, "close": 99.8})
        rows[30].update({"open": 99.8, "high": 100.8, "low": 99.6, "close": 100.7})
        return pd.DataFrame(rows)

    def test_profile_flags_control_ob_bos_and_sweep_requirements(self):
        engine = TAEngine()
        strict = engine.add_indicators(
            self._bars_without_bos_or_sweep(),
            filter_profile={"require_bos": True, "require_sweep": True},
        )
        flexible = engine.add_indicators(
            self._bars_without_bos_or_sweep(),
            filter_profile={"require_bos": False, "require_sweep": False},
        )

        self.assertTrue(pd.isna(strict.loc[30, "ob_bullish_low"]))
        self.assertAlmostEqual(float(flexible.loc[30, "ob_bullish_low"]), 99.5)
        self.assertAlmostEqual(float(flexible.loc[30, "ob_bullish_high"]), 100.0)

    def test_htf_api_failure_is_unknown_not_neutral(self):
        client = Mock()
        client.fetch_ohlcv.return_value = None

        result = TAEngine().get_htf_trend("NO_CACHE/USDT", client)

        self.assertEqual(result, "unknown")

    def test_consumed_fvg_is_not_reused_as_market_target(self):
        frame = pd.DataFrame({
            "timestamp": pd.date_range("2026-08-20", periods=10, freq="15min"),
            "open": [100.0] * 10,
            "high": [101.0] * 10,
            "low": [99.0] * 10,
            "close": [100.0] * 10,
            "fvg_bullish_created": [False, False, True] + [False] * 7,
            "fvg_bullish_low": [0.0, 0.0, 100.0] + [0.0] * 7,
            "fvg_bullish_high": [0.0, 0.0, 101.0] + [0.0] * 7,
            "fvg_bearish_created": [False] * 10,
            "bullish_ob_created": [False] * 10,
            "bearish_ob_created": [False] * 10,
        })
        # شمعة لاحقة تداولت داخل الفجوة، لذا لا تبقى هدفاً "نظيفاً".
        frame.loc[4, "low"] = 100.5

        levels = TAEngine().extract_market_levels(frame)

        self.assertEqual(levels["bullish_fvgs"], [])


if __name__ == "__main__":
    unittest.main()
