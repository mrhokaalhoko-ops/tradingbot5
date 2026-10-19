import unittest
import os
import tempfile
import sqlite3
import utils.forensics_logger as forensics

class TestForensicsLogger(unittest.TestCase):
    def setUp(self):
        # قاعدة بيانات مؤقتة منفصلة لكل اختبار
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        forensics.DB_PATH = self.tmp.name
        forensics.init_forensics_table()

    def tearDown(self):
        forensics.close_conn()
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_init_creates_table_and_indexes(self):
        # التحقق من وجود الجدول والفهارس
        conn = sqlite3.connect(self.tmp.name)
        cur = conn.cursor()
        
        # تحقق من الجدول
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signal_forensics'")
        self.assertIsNotNone(cur.fetchone())

        # تحقق من الفهارس
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signal_forensics'")
        indexes = [row[0] for row in cur.fetchall()]
        self.assertIn("idx_forensics_timestamp", indexes)
        self.assertIn("idx_forensics_symbol", indexes)
        self.assertIn("idx_forensics_outcome", indexes)
        conn.close()

    def test_log_signal_and_cleanup(self):
        # تسجيل إشارة تجريبية
        ta_data = {"score": 0.85, "rsi": 62.0, "adx": 28.0, "htf_trend": "bullish"}
        sentiment = {"label": "positive", "score": 0.9}
        trade_id = "test_tr_123"
        
        forensics.log_signal(
            symbol="BTCUSDT",
            signal="buy",
            reason="SMC Bullish Breakout",
            ta_data=ta_data,
            sentiment=sentiment,
            action_taken="executed",
            reject_reason="",
            trade_id=trade_id
        )

        # التحقق من إدخال البيانات بنجاح
        conn = sqlite3.connect(self.tmp.name)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM signal_forensics WHERE symbol='BTCUSDT'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["signal"], "buy")
        self.assertEqual(row["action_taken"], "executed")
        self.assertEqual(row["trade_id"], trade_id)
        self.assertAlmostEqual(row["ta_score"], 0.85)
        self.assertAlmostEqual(row["sentiment_score"], 0.9)
        self.assertEqual(row["sentiment_label"], "positive")
        self.assertIsNone(row["outcome"])
        conn.close()

        # تحديث النتيجة
        forensics.update_signal_outcome(trade_id, "TAKE_PROFIT", 15.5)

        # التحقق من تحديث النتيجة بنجاح
        conn = sqlite3.connect(self.tmp.name)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM signal_forensics WHERE symbol='BTCUSDT'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], "TAKE_PROFIT")
        self.assertAlmostEqual(row["pnl"], 15.5)

        # تجربة التنظيف (cleanup) لسجل قديم
        # سنحدث الوقت يدوياً ليكون أقدم من 30 يوم
        cur.execute("UPDATE signal_forensics SET timestamp='2020-01-01 12:00:00'")
        conn.commit()
        conn.close()

        # تشغيل دالة التنظيف
        forensics.cleanup_old_forensics(keep_days=30)

        # التحقق من حذف السجل القديم
        conn = sqlite3.connect(self.tmp.name)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM signal_forensics")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_root_failure_is_not_overwritten_by_later_close_trigger(self):
        trade_id = "root-cause-1"
        forensics.log_signal(
            symbol="ONDOUSDT", signal="buy", reason="test", ta_data={},
            sentiment={}, action_taken="executed", trade_id=trade_id,
        )
        forensics.update_signal_outcome(
            trade_id,
            "PROTECTION_FAILED_EMERGENCY_CLOSE",
            -1.0,
            root_cause="PROTECTION_FAILED",
        )
        forensics.update_signal_outcome(
            trade_id,
            "MANUAL_CLOSE",
            -1.2,
            close_trigger="MANUAL_CLOSE",
        )

        conn = sqlite3.connect(self.tmp.name)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM signal_forensics WHERE trade_id=?", (trade_id,)
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_events WHERE trade_id=?", (trade_id,)
        ).fetchone()[0]
        conn.close()

        self.assertEqual(row["root_outcome"], "PROTECTION_FAILED")
        self.assertEqual(row["close_trigger"], "MANUAL_CLOSE")
        self.assertEqual(row["terminal_outcome"], "MANUAL_CLOSE")
        self.assertEqual(event_count, 2)

if __name__ == "__main__":
    unittest.main()
