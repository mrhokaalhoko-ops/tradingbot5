"""
SQLite Database Manager — بديل CSV لتسجيل الصفقات
=======================================================
- يُنشئ قاعدة البيانات وجداولها تلقائياً عند أول تشغيل
- يُوفر دالة migrate_from_csv() لترحيل البيانات القديمة
- thread-safe عبر check_same_thread=False + قفل threading.Lock
- الـ PerformanceTracker يقرأ منه مباشرة بدل CSV
"""
import json
import os
import sqlite3
import threading
import pandas as pd
from datetime import date, datetime
from core.config import Config
from core.logger import logger

DB_PATH = os.getenv("TRADES_DB", os.path.join(Config.DATA_DIR, "trades.db"))

_lock = threading.Lock()
_conn: sqlite3.Connection = None

# ------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id              TEXT UNIQUE NOT NULL,
    symbol                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    entry_time            TEXT,
    entry_price           REAL DEFAULT 0,
    exit_time             TEXT,
    exit_price            REAL DEFAULT 0,
    pnl                   REAL DEFAULT 0,
    pnl_pct               REAL DEFAULT 0,
    exit_reason           TEXT DEFAULT '',
    entry_reason          TEXT DEFAULT '',
    strategy_source       TEXT DEFAULT '',
    sentiment_score       REAL DEFAULT 0,
    adx                   REAL DEFAULT 0,
    ema_200               REAL DEFAULT 0,
    atr                   REAL DEFAULT 0,
    spread                REAL DEFAULT 0,
    volume_24h            REAL DEFAULT 0,
    risk_pct              REAL DEFAULT 0,
    leverage              REAL DEFAULT 1,
    slippage              REAL DEFAULT 0,
    amount                REAL DEFAULT 0,
    filter_profile        TEXT DEFAULT '',
    journal_dir           TEXT DEFAULT '',
    wallet_equity_at_entry REAL DEFAULT 0,
    wallet_pnl_pct        REAL DEFAULT 0,
    reference_balance     REAL DEFAULT 50,
    pnl_ref_50            REAL DEFAULT 0,
    setup_thesis_id       TEXT DEFAULT '',
    market_regime         TEXT DEFAULT 'UNKNOWN',
    target_type           TEXT DEFAULT '',
    planned_rr            REAL DEFAULT 0,
    mfe_r                 REAL DEFAULT 0,
    mae_r                 REAL DEFAULT 0,
    final_r               REAL DEFAULT 0,
    realized_pnl_gross    REAL DEFAULT 0,
    entry_commission      REAL DEFAULT 0,
    exit_commission       REAL DEFAULT 0,
    commission            REAL DEFAULT 0,
    funding               REAL DEFAULT 0,
    funding_status        TEXT DEFAULT 'UNKNOWN',
    pnl_complete          INTEGER DEFAULT 0,
    created_at            TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_exit_time  ON trades(exit_time);
CREATE INDEX IF NOT EXISTS idx_trades_symbol      ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_trade_id    ON trades(trade_id);
"""

ALLOWED_COLUMNS = {
    "trade_id", "symbol", "side", "entry_time", "entry_price",
    "exit_time", "exit_price", "pnl", "pnl_pct", "exit_reason",
    "entry_reason", "strategy_source", "sentiment_score", "adx", "ema_200", "atr",
    "spread", "volume_24h", "risk_pct", "leverage", "slippage",
    "amount", "filter_profile", "journal_dir", "wallet_equity_at_entry",
    "wallet_pnl_pct", "reference_balance", "pnl_ref_50",
    "setup_thesis_id", "market_regime", "target_type", "planned_rr",
    "mfe_r", "mae_r", "final_r", "realized_pnl_gross",
    "entry_commission", "exit_commission", "commission", "funding",
    "funding_status", "pnl_complete",
}

# ------------------------------------------------------------------
# Connection
# ------------------------------------------------------------------

_current_db_path = None

def _get_conn() -> sqlite3.Connection:
    global _conn, _current_db_path
    if _conn is None or _current_db_path != DB_PATH:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.execute("PRAGMA wal_autocheckpoint=1000")
        _current_db_path = DB_PATH
    return _conn


def close_conn():
    global _conn, _current_db_path
    with _lock:
        if _conn is not None:
            try:
                _conn.commit()
                _conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                _conn.close()
            except Exception:
                pass
            _conn = None
            _current_db_path = None


def init_db():
    """يُنشئ قاعدة البيانات والجداول إذا لم تكن موجودة."""
    with _lock:
        try:
            conn = _get_conn()
            conn.executescript(SCHEMA)
            # ترحيل آمن: إضافة أعمدة جديدة لقاعدة بيانات قائمة دون حذف بياناتها
            for col_def in [
                "ALTER TABLE trades ADD COLUMN strategy_source TEXT DEFAULT ''",
                "ALTER TABLE trades ADD COLUMN setup_thesis_id TEXT DEFAULT ''",
                "ALTER TABLE trades ADD COLUMN market_regime TEXT DEFAULT 'UNKNOWN'",
                "ALTER TABLE trades ADD COLUMN target_type TEXT DEFAULT ''",
                "ALTER TABLE trades ADD COLUMN planned_rr REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN mfe_r REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN mae_r REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN final_r REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN realized_pnl_gross REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN entry_commission REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN exit_commission REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN commission REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN funding REAL DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN funding_status TEXT DEFAULT 'UNKNOWN'",
                "ALTER TABLE trades ADD COLUMN pnl_complete INTEGER DEFAULT 0",
            ]:
                try:
                    conn.execute(col_def)
                except Exception:
                    pass  # العمود موجود بالفعل — تجاهل الخطأ بأمان
            conn.commit()
            logger.info(f"[DB] قاعدة بيانات SQLite جاهزة: {DB_PATH}")
        except Exception as e:
            logger.error(f"[DB] فشل تهيئة قاعدة البيانات: {e}")


# ------------------------------------------------------------------
# Write
# ------------------------------------------------------------------

def _sqlite_safe_value(value):
    """Convert persisted metadata to a deterministic SQLite scalar.

    Trading metadata legitimately contains nested dictionaries (for example
    ``market_regime``). Letting those objects cross the database boundary
    makes sqlite3 reject the whole close record. Semantic producers should
    still flatten fields where appropriate; this is the final defensive wall.
    """
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, (str, bytes, int, float)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()

    # numpy/pandas scalar types are not guaranteed to be accepted by sqlite3.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _sqlite_safe_value(item())
        except (TypeError, ValueError):
            pass

    if isinstance(value, (dict, list, tuple, set)):
        serializable = sorted(value) if isinstance(value, set) else value
        return json.dumps(
            serializable,
            ensure_ascii=False,
            sort_keys=isinstance(serializable, dict),
            separators=(",", ":"),
            default=str,
        )
    return str(value)

def insert_trade(row: dict) -> bool:
    """
    يُدرج صفقة واحدة في قاعدة البيانات.
    يتجاهل التكرار بصمت (ON CONFLICT IGNORE).
    """
    with _lock:
        try:
            filtered = {
                k: _sqlite_safe_value(v)
                for k, v in row.items()
                if k in ALLOWED_COLUMNS
            }
            if not filtered:
                return False
            conn = _get_conn()
            cols = ", ".join(filtered.keys())
            placeholders = ", ".join(["?" for _ in filtered])
            sql = f"INSERT OR IGNORE INTO trades ({cols}) VALUES ({placeholders})"
            conn.execute(sql, list(filtered.values()))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"[DB] فشل إدراج الصفقة: {e}")
            return False


# ------------------------------------------------------------------
# Read
# ------------------------------------------------------------------

def load_trades_df() -> pd.DataFrame:
    """
    يُعيد جميع الصفقات المغلقة كـ DataFrame مرتبة حسب exit_time.
    يُستخدم من PerformanceTracker بدلاً من pd.read_csv().
    """
    try:
        if not os.path.exists(DB_PATH):
            return _empty_df()
        with _lock:
            conn = _get_conn()
            df = pd.read_sql_query(
                "SELECT * FROM trades WHERE exit_time IS NOT NULL AND exit_time != '' ORDER BY exit_time ASC",
                conn
            )

        if df.empty:
            return _empty_df()

        # تحويل أنواع البيانات
        df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
        df["exit_time"]  = pd.to_datetime(df["exit_time"],  errors="coerce")

        numeric_cols = [
            "entry_price", "exit_price", "pnl", "pnl_pct", "amount",
            "risk_pct", "leverage", "slippage", "wallet_equity_at_entry",
            "wallet_pnl_pct", "reference_balance", "pnl_ref_50",
            "planned_rr", "mfe_r", "mae_r", "final_r",
            "realized_pnl_gross", "entry_commission", "exit_commission",
            "commission", "funding", "pnl_complete",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

        df["symbol"] = df["symbol"].fillna("").astype(str)
        df["side"]   = df["side"].fillna("").astype(str).str.upper()

        # حساب حقول مشتقة (مثل PerformanceTracker)
        df["entry_notional"] = (df["entry_price"] * df["amount"]).abs()
        df["leverage"] = df["leverage"].replace(0, 1).clip(lower=1)
        df["margin_used"] = df["entry_notional"] / df["leverage"]
        df["roe_pct"] = (
            df["pnl"].div(df["margin_used"])
            .mul(100)
            .where(df["margin_used"] > 0, other=0.0)
        )

        return df

    except Exception as e:
        logger.error(f"[DB] فشل قراءة الصفقات: {e}")
        return _empty_df()


def trade_exists(trade_id: str) -> bool:
    """يتحقق إذا كانت الصفقة مسجلة مسبقاً (لمنع التكرار)."""
    try:
        if not os.path.exists(DB_PATH):
            return False
        with _lock:
            conn = _get_conn()
            row = conn.execute(
                "SELECT 1 FROM trades WHERE trade_id = ? LIMIT 1", (trade_id,)
            ).fetchone()
            return row is not None
    except Exception:
        return False


def _empty_df() -> pd.DataFrame:
    cols = [
        "trade_id", "symbol", "side", "entry_time", "entry_price",
        "exit_time", "exit_price", "pnl", "pnl_pct", "exit_reason",
        "entry_reason", "amount", "risk_pct", "leverage", "slippage",
        "wallet_equity_at_entry", "wallet_pnl_pct", "reference_balance",
        "pnl_ref_50", "filter_profile", "journal_dir", "setup_thesis_id",
        "market_regime", "target_type", "planned_rr", "mfe_r", "mae_r",
        "final_r", "realized_pnl_gross", "entry_commission",
        "exit_commission", "commission", "funding", "funding_status",
        "pnl_complete", "roe_pct",
    ]
    return pd.DataFrame(columns=cols)


# ------------------------------------------------------------------
# Migration: CSV → SQLite
# ------------------------------------------------------------------

def migrate_from_csv(csv_path: str) -> int:
    """
    يُرحّل الصفقات من ملف CSV القديم إلى SQLite.
    يتجاهل الصفقات المكررة (trade_id موجود مسبقاً).
    يُعيد عدد الصفقات التي تم ترحيلها.
    """
    if not os.path.exists(csv_path):
        logger.info(f"[DB] لا يوجد ملف CSV للترحيل: {csv_path}")
        return 0
    if os.path.getsize(csv_path) == 0:
        logger.info(f"[DB] ملف CSV فارغ؛ لا توجد بيانات للترحيل: {csv_path}")
        return 0

    try:
        malformed_lines = []

        def _skip_malformed(fields):
            malformed_lines.append(1)
            return None

        df = pd.read_csv(
            csv_path,
            engine="python",
            on_bad_lines=_skip_malformed,
        )
        required_columns = {"trade_id", "exit_time"}
        missing = required_columns.difference(df.columns)
        if missing:
            logger.error(f"[DB] CSV غير صالح للترحيل؛ أعمدة مطلوبة مفقودة: {sorted(missing)}")
            return 0
        df = df[df["exit_time"].notna()].copy()

        rows_to_insert = []
        skipped_rows = 0

        def _float_value(row, key, default=0.0):
            value = row.get(key, default)
            if pd.isna(value) or value == "":
                return float(default)
            return float(value)

        for row_number, row in df.iterrows():
            try:
                trade_id = str(row.get("trade_id", "")).strip()
                exit_time = str(row.get("exit_time", "")).strip()
                if not trade_id or trade_id.lower() == "nan" or not exit_time:
                    raise ValueError("trade_id/exit_time مفقود")
                trade_row = {
                "trade_id":              str(row.get("trade_id", "")),
                "symbol":               str(row.get("symbol", "")),
                "side":                 str(row.get("side", "")),
                "entry_time":           str(row.get("entry_time", "")),
                "entry_price":          _float_value(row, "entry_price"),
                "exit_time":            str(row.get("exit_time", "")),
                "exit_price":           _float_value(row, "exit_price"),
                "pnl":                  _float_value(row, "pnl"),
                "pnl_pct":              _float_value(row, "pnl_pct"),
                "exit_reason":          str(row.get("exit_reason", "")),
                "entry_reason":         str(row.get("entry_reason", "")),
                "sentiment_score":      _float_value(row, "sentiment_score"),
                "adx":                  _float_value(row, "adx"),
                "ema_200":              _float_value(row, "ema_200"),
                "atr":                  _float_value(row, "atr"),
                "spread":               _float_value(row, "spread"),
                "volume_24h":           _float_value(row, "volume_24h"),
                "risk_pct":             _float_value(row, "risk_pct"),
                "leverage":             _float_value(row, "leverage", 1),
                "slippage":             _float_value(row, "slippage"),
                "amount":               _float_value(row, "amount"),
                "filter_profile":       str(row.get("filter_profile", "")),
                "journal_dir":          str(row.get("journal_dir", "")),
                "wallet_equity_at_entry": _float_value(row, "wallet_equity_at_entry"),
                "wallet_pnl_pct":       _float_value(row, "wallet_pnl_pct"),
                "reference_balance":    _float_value(row, "reference_balance", 50),
                "pnl_ref_50":           _float_value(row, "pnl_ref_50"),
                "strategy_source":      str(row.get("strategy_source", "unknown")),
                }
                rows_to_insert.append(trade_row)
            except (TypeError, ValueError) as exc:
                skipped_rows += 1
                logger.warning(f"[DB] تخطي صف CSV تالف رقم {row_number + 2}: {exc}")

        if not rows_to_insert:
            return 0

        with _lock:
            conn = _get_conn()
            changes_before = conn.total_changes
            cols = list(rows_to_insert[0].keys())
            placeholders = ", ".join(["?" for _ in cols])
            sql = f"INSERT OR IGNORE INTO trades ({', '.join(cols)}) VALUES ({placeholders})"
            conn.executemany(sql, [list(r.values()) for r in rows_to_insert])
            conn.commit()
            migrated = conn.total_changes - changes_before

        logger.info(
            f"[DB] ترحيل CSV: migrated={migrated} | malformed_lines={len(malformed_lines)} "
            f"| invalid_rows={skipped_rows}"
        )
        return migrated

    except Exception as e:
        logger.error(f"[DB] فشل ترحيل CSV: {e}")
        return 0


# ------------------------------------------------------------------
# Protection Queries
# ------------------------------------------------------------------

def get_recent_trades(limit: int = 100) -> list:
    """جلب آخر N صفقة مغلقة من SQLite — تستخدمها دوال الحماية"""
    with _lock:
        try:
            conn = _get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trades WHERE exit_time IS NOT NULL AND exit_time != '' ORDER BY exit_time DESC LIMIT ?",
                (limit,)
            )
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            logger.error(f"[DB] فشل جلب الصفقات الأخيرة: {e}")
            return []
