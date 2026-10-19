"""
Forensics Logger — يسجّل كل إشارة وكل رفض في SQLite
لماذا دُخل؟ لماذا رُفض؟ — للمراجعة التفصيلية لأداء البوت
"""
import sqlite3
import os
import threading
import json
from datetime import datetime
from core.config import Config
from core.logger import logger

DB_PATH = os.getenv("TRADES_DB", os.path.join(Config.DATA_DIR, "trades.db"))

_conn: sqlite3.Connection = None
_lock = threading.Lock()

def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("PRAGMA busy_timeout=5000;")
        _conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return _conn

def close_conn():
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.commit()
                _conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                _conn.close()
            except Exception:
                pass
            _conn = None


def init_forensics_table():
    """ينشئ جدول التشريح إذا لم يكن موجوداً (WAL mode) مع الفهارس المناسبة"""
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with _lock:
            conn = _get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_forensics (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL,
                    symbol           TEXT NOT NULL,
                    signal           TEXT NOT NULL,   -- buy / sell / hold
                    reason           TEXT,
                    ta_score         REAL DEFAULT 0,
                    rsi              REAL DEFAULT 0,
                    adx              REAL DEFAULT 0,
                    htf_trend        TEXT DEFAULT 'neutral',
                    macd_cross_since INTEGER,
                    rsi_cross_since  INTEGER,
                    ob_bullish_age   REAL,
                    ob_bearish_age   REAL,
                    sentiment_label  TEXT DEFAULT 'neutral',
                    sentiment_score  REAL DEFAULT 0,
                    action_taken     TEXT,            -- candidate / rejected / held (execution is an outcome event)
                    reject_reason    TEXT,
                    trade_id         TEXT,             -- يربط بسجل TradeJournal/CSV لاحقاً
                    outcome          TEXT DEFAULT NULL, -- TAKE_PROFIT / STOP_LOSS / NULL (لم تُغلق بعد)
                    pnl              REAL DEFAULT NULL,
                    root_outcome     TEXT DEFAULT NULL,
                    close_trigger    TEXT DEFAULT NULL,
                    terminal_outcome TEXT DEFAULT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_lifecycle_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT NOT NULL,
                    trade_id    TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    outcome     TEXT,
                    pnl         REAL,
                    details_json TEXT
                )
            """)
            
            # فحص الأعمدة وإضافتها إذا لم تكن موجودة (ترحيل ديناميكي SQLite Migration)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(signal_forensics);")
            columns = [row[1] for row in cursor.fetchall()]
            
            if "trade_id" not in columns:
                conn.execute("ALTER TABLE signal_forensics ADD COLUMN trade_id TEXT;")
            if "outcome" not in columns:
                conn.execute("ALTER TABLE signal_forensics ADD COLUMN outcome TEXT DEFAULT NULL;")
            if "pnl" not in columns:
                conn.execute("ALTER TABLE signal_forensics ADD COLUMN pnl REAL DEFAULT NULL;")
            if "root_outcome" not in columns:
                conn.execute("ALTER TABLE signal_forensics ADD COLUMN root_outcome TEXT DEFAULT NULL;")
            if "close_trigger" not in columns:
                conn.execute("ALTER TABLE signal_forensics ADD COLUMN close_trigger TEXT DEFAULT NULL;")
            if "terminal_outcome" not in columns:
                conn.execute("ALTER TABLE signal_forensics ADD COLUMN terminal_outcome TEXT DEFAULT NULL;")
                
            conn.execute("CREATE INDEX IF NOT EXISTS idx_forensics_timestamp ON signal_forensics(timestamp DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_forensics_symbol ON signal_forensics(symbol);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_forensics_outcome ON signal_forensics(outcome);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_trade_id ON trade_lifecycle_events(trade_id, id);")
            conn.commit()
    except Exception as e:
        logger.error(f"[Forensics] فشل إنشاء الجدول: {e}")


def log_signal(symbol: str, signal: str, reason: str, ta_data: dict,
               sentiment: dict, action_taken: str, reject_reason: str = "", trade_id: str = None):
    """
    يُسجّل إشارة واحدة مع كل تفاصيلها للمراجعة لاحقاً.
    استدعه في كل مرة يتخذ فيها البوت قراراً (حتى قرار الانتظار).
    """
    try:
        with _lock:
            conn = _get_conn()
            conn.execute("""
                INSERT INTO signal_forensics
                (timestamp, symbol, signal, reason, ta_score, rsi, adx, htf_trend,
                 macd_cross_since, rsi_cross_since, ob_bullish_age, ob_bearish_age,
                 sentiment_label, sentiment_score, action_taken, reject_reason, trade_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                symbol, signal, reason,
                float(ta_data.get("score",         0) or 0),
                float(ta_data.get("rsi",           0) or 0),
                float(ta_data.get("adx",           0) or 0),
                str(ta_data.get("htf_trend",  "neutral")),
                ta_data.get("macd_cross_since"),
                ta_data.get("rsi_cross_since"),
                ta_data.get("ob_bullish_age"),
                ta_data.get("ob_bearish_age"),
                str(sentiment.get("label",    "neutral")),
                float(sentiment.get("score",       0) or 0),
                action_taken,
                reject_reason,
                trade_id,
            ))
            conn.commit()
    except Exception as e:
        logger.error(f"[Forensics] فشل تسجيل الإشارة لـ {symbol}: {e}")


def update_signal_outcome(
    trade_id: str,
    outcome: str,
    pnl: float,
    root_cause: str = None,
    close_trigger: str = None,
    details: dict = None,
):
    """يسجل حدثاً جديداً ويحافظ على السبب الجذري الأول للصفقة.

    ``outcome`` يبقى محدثاً للتوافق مع التقارير القديمة، لكن سجل الأحداث لا
    يُكتب فوقه، وroot_outcome لا يمكن أن يُطمس بإغلاق لاحق.
    """
    if not trade_id:
        return
    try:
        with _lock:
            conn = _get_conn()
            event_type = (
                "CLOSE_TRIGGER" if close_trigger else
                "ROOT_CAUSE" if root_cause else
                "OUTCOME"
            )
            conn.execute("""
                INSERT INTO trade_lifecycle_events
                (timestamp, trade_id, event_type, outcome, pnl, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                trade_id,
                event_type,
                outcome,
                float(pnl or 0),
                json.dumps(details or {}, ensure_ascii=False),
            ))
            conn.execute("""
                UPDATE signal_forensics
                SET outcome = ?,
                    pnl = ?,
                    root_outcome = CASE
                        WHEN root_outcome IS NULL OR root_outcome = ''
                        THEN COALESCE(?, root_outcome)
                        ELSE root_outcome
                    END,
                    close_trigger = COALESCE(?, close_trigger),
                    terminal_outcome = ?
                WHERE trade_id = ?
            """, (
                outcome,
                float(pnl or 0),
                root_cause,
                close_trigger,
                outcome,
                trade_id,
            ))
            conn.commit()
            logger.info(f"[Forensics] تم تحديث نتيجة الإشارة للصفقة {trade_id} بنجاح: {outcome} | PnL: {pnl}")
    except Exception as e:
        logger.error(f"[Forensics] فشل تحديث نتيجة الصفقة {trade_id} في التشريح: {e}")


def cleanup_old_forensics(keep_days: int = 30):
    """يحذف سجلات أقدم من keep_days يوم"""
    try:
        with _lock:
            conn = _get_conn()
            signals_cursor = conn.execute("""
                DELETE FROM signal_forensics
                WHERE timestamp < datetime('now', ? || ' days')
            """, (f"-{keep_days}",))
            events_cursor = conn.execute("""
                DELETE FROM trade_lifecycle_events
                WHERE timestamp < datetime('now', ? || ' days')
            """, (f"-{keep_days}",))
            conn.commit()
            deleted = max(signals_cursor.rowcount, 0) + max(events_cursor.rowcount, 0)
            if deleted > 0:
                logger.info(f"[Forensics] تم حذف {deleted} سجل قديم (أقدم من {keep_days} يوم)")
    except Exception as e:
        logger.error(f"[Forensics] فشل تنظيف السجلات القديمة: {e}")
