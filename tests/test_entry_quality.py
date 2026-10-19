import unittest

import numpy as np
import pandas as pd

from engines.ta_engine import TAEngine


class TestDirectionalExtensionGuard(unittest.TestCase):
    @staticmethod
    def _quiet_frame():
        return pd.DataFrame({
            "open": np.full(10, 99.5),
            "high": np.full(10, 100.0),
            "low": np.full(10, 99.0),
            "close": np.full(10, 99.7),
            "atr": np.full(10, 1.0),
            "volume": np.full(10, 1000.0),
        })

    def test_breakout_outside_closed_candle_is_not_pinned_to_extreme_zone(self):
        audit = {}
        ok, reason = TAEngine().validate_entry_quality(
            self._quiet_frame(),
            "buy",
            current_price=100.01,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
            audit_details=audit,
        )

        self.assertTrue(ok, reason)
        self.assertNotEqual(audit.get("terminal_gate"), "EXTREME_ZONE")
        self.assertNotIn("closed_candle_position", audit.get("values", {}))
        extreme_gate = next(
            gate for gate in audit["gates"] if gate["gate"] == "EXTREME_ZONE"
        )
        self.assertEqual(
            extreme_gate["status"], "DEFERRED_TO_ENTRY_LOCATION_GUARDS"
        )
        self.assertFalse(extreme_gate["evaluated"])
        self.assertEqual(
            audit["values"]["reference_candle_relation"], "ABOVE_HIGH"
        )

    def test_breakdown_outside_closed_candle_is_not_pinned_to_extreme_zone(self):
        audit = {}
        ok, reason = TAEngine().validate_entry_quality(
            self._quiet_frame(),
            "sell",
            current_price=98.99,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
            audit_details=audit,
        )

        self.assertTrue(ok, reason)
        self.assertNotEqual(audit.get("terminal_gate"), "EXTREME_ZONE")
        self.assertNotIn("closed_candle_position", audit.get("values", {}))
        extreme_gate = next(
            gate for gate in audit["gates"] if gate["gate"] == "EXTREME_ZONE"
        )
        self.assertEqual(
            extreme_gate["status"], "DEFERRED_TO_ENTRY_LOCATION_GUARDS"
        )
        self.assertFalse(extreme_gate["evaluated"])
        self.assertEqual(
            audit["values"]["reference_candle_relation"], "BELOW_LOW"
        )

    def test_price_inside_closed_candle_extreme_remains_rejected(self):
        audit = {}
        ok, reason = TAEngine().validate_entry_quality(
            self._quiet_frame(),
            "buy",
            current_price=99.90,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
            audit_details=audit,
        )

        self.assertFalse(ok)
        self.assertEqual(audit.get("terminal_gate"), "EXTREME_ZONE")
        self.assertIn("أعلى", reason)

    def test_large_live_spike_is_audited_as_deferred_not_as_execution_approval(self):
        rows = 100
        frame = pd.DataFrame({
            "open": np.full(rows, 99.8),
            "high": np.full(rows, 100.2),
            "low": np.full(rows, 99.5),
            "close": np.full(rows, 100.0),
            "atr": np.full(rows, 1.0),
            "volume": np.full(rows, 1000.0),
        })
        frame.loc[rows - 2, ["open", "high", "low", "close"]] = [
            99.9, 100.3, 99.7, 100.1
        ]
        audit = {}

        ok, reason = TAEngine().validate_entry_quality(
            frame,
            "buy",
            current_price=105.0,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
            audit_details=audit,
        )

        self.assertTrue(ok, reason)
        self.assertAlmostEqual(
            audit["values"]["outside_reference_distance_atr"], 4.7
        )
        extreme_gate = next(
            gate for gate in audit["gates"] if gate["gate"] == "EXTREME_ZONE"
        )
        self.assertEqual(
            extreme_gate["status"], "DEFERRED_TO_ENTRY_LOCATION_GUARDS"
        )
        self.assertFalse(extreme_gate["evaluated"])

    def test_nan_reference_high_low_fails_closed(self):
        frame = self._quiet_frame()
        frame.loc[len(frame) - 2, ["high", "low"]] = [np.nan, np.nan]
        audit = {}

        ok, reason = TAEngine().validate_entry_quality(
            frame,
            "buy",
            current_price=100.5,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
            audit_details=audit,
        )

        self.assertFalse(ok)
        self.assertEqual(audit.get("terminal_gate"), "REFERENCE_CANDLE_DATA")
        self.assertIn("غير صالحة", reason)

    def test_inverted_reference_high_low_fails_closed(self):
        frame = self._quiet_frame()
        frame.loc[len(frame) - 2, ["high", "low"]] = [98.5, 99.0]
        audit = {}

        ok, reason = TAEngine().validate_entry_quality(
            frame,
            "sell",
            current_price=99.0,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
            audit_details=audit,
        )

        self.assertFalse(ok)
        self.assertEqual(audit.get("terminal_gate"), "REFERENCE_CANDLE_DATA")
        self.assertIn("غير صالحة", reason)

    def test_rejects_long_after_large_multi_atr_extension_near_range_high(self):
        rows = 100
        closes = np.linspace(100.0, 114.0, rows)
        frame = pd.DataFrame({
            "open": closes - 0.1,
            "high": closes + 0.3,
            "low": closes - 0.3,
            "close": closes,
            "atr": np.ones(rows),
            "volume": np.full(rows, 1000.0),
        })
        # آخر صف حي، والصف قبله هو المرجع المغلق. نجعله واسعاً بما يكفي
        # كي لا يرفضه فلتر موضع الشمعة قبل الوصول إلى فلتر الامتداد اليومي.
        frame.loc[rows - 2, ["open", "high", "low", "close"]] = [
            113.5, 116.0, 110.0, 114.0
        ]
        frame.loc[rows - 3, ["open", "high", "low", "close"]] = [
            113.4, 114.0, 113.0, 113.7
        ]

        ok, reason = TAEngine().validate_entry_quality(
            frame,
            "buy",
            current_price=114.0,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
        )

        self.assertFalse(ok)
        self.assertIn("امتداد", reason)
        self.assertIn("LONG", reason)

    def test_aligned_strong_regime_does_not_label_expansion_as_exhaustion(self):
        frame = pd.DataFrame({
            "open": np.full(10, 100.0),
            "high": np.full(10, 101.0),
            "low": np.full(10, 99.0),
            "close": np.full(10, 100.2),
            "atr": np.full(10, 1.0),
            "volume": np.full(10, 1000.0),
        })
        frame.loc[7, ["open", "high", "low", "close"]] = [
            100.0, 102.4, 99.8, 102.0
        ]
        frame.loc[8, ["open", "high", "low", "close"]] = [
            101.5, 103.0, 100.5, 102.0
        ]

        engine = TAEngine()
        blocked, blocked_reason = engine.validate_entry_quality(
            frame,
            "buy",
            current_price=101.8,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
        )
        allowed, allowed_reason = engine.validate_entry_quality(
            frame,
            "buy",
            current_price=101.8,
            filter_profile={"min_atr_pct": 0.005, "label": "test"},
            market_regime={"label": "STRONG_RISK_ON"},
        )

        self.assertFalse(blocked)
        self.assertIn("الشمعة السابقة", blocked_reason)
        self.assertTrue(allowed, allowed_reason)


if __name__ == "__main__":
    unittest.main()
