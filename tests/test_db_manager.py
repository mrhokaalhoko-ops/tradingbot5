"""
Unit Tests — db_manager
يختبر: إنشاء الجدول، إدراج الصفقات، منع التكرار، الترحيل من CSV
لا يتصل بأي API خارجي — يعمل على قاعدة بيانات مؤقتة في الذاكرة.
"""
import os
import sys
import csv
import tempfile
import unittest
from datetime import datetime, timezone

# أضف مسار المشروع حتى يعمل الاستيراد
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── تجاوز مسار DB بقاعدة بيانات مؤقتة ────────────────────────────
import utils.db_manager as db_mod

class TestDBManager(unittest.TestCase):

    def setUp(self):
        """قاعدة بيانات مؤقتة لكل اختبار"""
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db_mod.DB_PATH = self.tmp.name
        db_mod.init_db()

    def tearDown(self):
        db_mod.close_conn()
        os.unlink(self.tmp.name)

    def _sample_trade(self, trade_id="T001", pnl=10.5):
        return {
            "trade_id":               trade_id,
            "symbol":                 "BTC/USDT",
            "side":                   "BUY",
            "entry_time":             "2024-01-01 10:00:00",
            "entry_price":            40000.0,
            "exit_time":              "2024-01-01 12:00:00",
            "exit_price":             41050.0,
            "pnl":                    pnl,
            "pnl_pct":                2.6,
            "exit_reason":            "TP1",
            "entry_reason":           "SMC_BUY",
            "sentiment_score":        0.7,
            "adx":                    28.5,
            "ema_200":                39000.0,
            "atr":                    500.0,
            "spread":                 0.001,
            "volume_24h":             1e9,
            "risk_pct":               1.0,
            "leverage":               10.0,
            "slippage":               0.0002,
            "amount":                 0.025,
            "filter_profile":         "balanced",
            "journal_dir":            "",
            "wallet_equity_at_entry": 50.0,
            "wallet_pnl_pct":         21.0,
            "reference_balance":      50.0,
            "pnl_ref_50":             10.5,
            "setup_thesis_id":        "setup-btc-1",
            "market_regime":          "STRONG_RISK_ON",
            "target_type":            "bearish_fvg",
            "planned_rr":             2.0,
            "mfe_r":                  1.4,
            "mae_r":                  0.3,
            "final_r":                0.8,
        }

    # ── 1. إدراج صفقة واحدة ───────────────────────────────────────
    def test_insert_trade_success(self):
        row = self._sample_trade()
        result = db_mod.insert_trade(row)
        self.assertTrue(result, "يجب أن يعود True عند إدراج صفقة جديدة")

    # ── 2. قراءة الصفقات بعد الإدراج ─────────────────────────────
    def test_load_trades_returns_dataframe(self):
        db_mod.insert_trade(self._sample_trade("T001", pnl=5.0))
        db_mod.insert_trade(self._sample_trade("T002", pnl=-2.0))
        df = db_mod.load_trades_df()
        self.assertEqual(len(df), 2, "يجب أن يُعيد صفقتين")
        self.assertIn("pnl", df.columns, "يجب وجود عمود pnl")
        self.assertIn("roe_pct", df.columns, "يجب وجود عمود roe_pct مُحسَب")
        self.assertAlmostEqual(df.iloc[0]["final_r"], 0.8)
        self.assertEqual(df.iloc[0]["market_regime"], "STRONG_RISK_ON")

    def test_nested_runtime_metadata_cannot_break_close_persistence(self):
        row = self._sample_trade("NESTED_META")
        row["market_regime"] = {
            "label": "STRONG_RISK_ON",
            "breadth": {"positive": 0.72},
        }
        row["entry_reason"] = ["SMC", "RECLAIM"]
        row["exit_time"] = datetime(2026, 8, 26, tzinfo=timezone.utc)

        self.assertTrue(db_mod.insert_trade(row))
        loaded = db_mod.get_recent_trades(limit=1)[0]
        self.assertIn('"label":"STRONG_RISK_ON"', loaded["market_regime"])
        self.assertEqual(loaded["entry_reason"], '["SMC","RECLAIM"]')
        self.assertTrue(str(loaded["exit_time"]).startswith("2026-08-26"))

    # ── 3. منع تكرار نفس الصفقة ──────────────────────────────────
    def test_no_duplicate_trades(self):
        row = self._sample_trade("DUP001")
        db_mod.insert_trade(row)
        db_mod.insert_trade(row)   # محاولة ثانية
        df = db_mod.load_trades_df()
        self.assertEqual(len(df), 1, "يجب ألا تُكرَّر الصفقة")

    # ── 4. trade_exists صحيح ─────────────────────────────────────
    def test_trade_exists(self):
        db_mod.insert_trade(self._sample_trade("EXISTS_001"))
        self.assertTrue(db_mod.trade_exists("EXISTS_001"))
        self.assertFalse(db_mod.trade_exists("NOT_IN_DB"))

    # ── 5. الترحيل من CSV ─────────────────────────────────────────
    def test_migrate_from_csv(self):
        fields = list(self._sample_trade().keys())
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.csv', delete=False, newline='', encoding='utf-8'
        ) as f:
            csv_path = f.name
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerow(self._sample_trade("CSV_T001", pnl=3.0))
            writer.writerow(self._sample_trade("CSV_T002", pnl=-1.0))

        try:
            migrated = db_mod.migrate_from_csv(csv_path)
            self.assertEqual(migrated, 2, "يجب ترحيل صفقتين")
            df = db_mod.load_trades_df()
            self.assertEqual(len(df), 2)
        finally:
            os.unlink(csv_path)

    # ── 6. الترحيل من CSV غير موجود ──────────────────────────────
    def test_migrate_nonexistent_csv(self):
        result = db_mod.migrate_from_csv("/nonexistent/path.csv")
        self.assertEqual(result, 0, "يجب إعادة 0 إذا لم يوجد ملف CSV")

    def test_migrate_empty_csv_is_a_clean_noop(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as handle:
            csv_path = handle.name

        try:
            self.assertEqual(db_mod.migrate_from_csv(csv_path), 0)
        finally:
            os.unlink(csv_path)

    # ── 7. roe_pct محسوب بشكل صحيح ───────────────────────────────
    def test_roe_pct_calculation(self):
        # pnl=10, entry=40000, amount=0.025, leverage=10
        # margin_used = (40000 * 0.025) / 10 = 100
        # roe_pct = 10 / 100 * 100 = 10%
        db_mod.insert_trade(self._sample_trade("ROE_001", pnl=10.0))
        df = db_mod.load_trades_df()
        self.assertAlmostEqual(df.iloc[0]["roe_pct"], 10.0, places=1)

    # ── 8. قاعدة بيانات فارغة تُعيد DataFrame فارغ ──────────────
    def test_empty_db_returns_empty_df(self):
        df = db_mod.load_trades_df()
        self.assertTrue(df.empty, "قاعدة بيانات فارغة يجب أن تُعيد DataFrame فارغ")

    def test_migration_skips_malformed_and_invalid_rows_but_keeps_good_rows(self):
        fields = list(self._sample_trade().keys())
        good = self._sample_trade("GOOD_ONLY", pnl=2.5)
        invalid = self._sample_trade("BAD_NUMBER", pnl=1.0)
        invalid["entry_price"] = "not-a-number"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
        ) as handle:
            csv_path = handle.name
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(good)
            handle.write(",".join(["too-many-fields"] * (len(fields) + 3)) + "\n")
            writer.writerow(invalid)

        try:
            self.assertEqual(db_mod.migrate_from_csv(csv_path), 1)
            rows = db_mod.load_trades_df()
            self.assertEqual(rows["trade_id"].tolist(), ["GOOD_ONLY"])
        finally:
            os.unlink(csv_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
