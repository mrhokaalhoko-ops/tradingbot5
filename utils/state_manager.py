import json
import os
import time
import threading
import tempfile
import copy
import uuid
from datetime import datetime, timedelta, timezone
from core.logger import logger

_DEFAULT_DATA_DIR = "." if os.name == "nt" else "/app/data"
STATE_FILE = os.getenv(
    "STATE_FILE",
    os.path.join(os.getenv("DATA_DIR", _DEFAULT_DATA_DIR), "bot_state.json"),
)

CURRENT_STATE_SCHEMA_VERSION = 3


class FutureStateSchemaError(ValueError):
    """The state belongs to newer code; an older process must stay read-only."""


def _migrate_state_dict(saved):
    """Return a migrated copy without mutating the object read from disk."""
    migrated = copy.deepcopy(saved)
    raw_version = migrated.get("schema_version", 1)
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid state schema_version={raw_version!r}") from exc

    if version > CURRENT_STATE_SCHEMA_VERSION:
        raise FutureStateSchemaError(
            f"state schema v{version} is newer than supported "
            f"v{CURRENT_STATE_SCHEMA_VERSION}"
        )
    if version < 1:
        raise ValueError(f"unsupported state schema v{version}")

    # v1 had no explicit version and stored only active order intents.  v2
    # keeps terminal proofs in a bounded archive so recovery never has to
    # equate an absent position with an order that certainly never filled.
    if version == 1:
        migrated.setdefault("archived_order_intents", {})
        migrated["schema_version"] = 2
        version = 2

    # v3 introduces durable close/protection sagas and an explicit UTC risk
    # session.  All fields are additive, but making the migration explicit
    # prevents an older binary from overwriting their meaning on rollback.
    if version == 2:
        migrated.setdefault("close_intents", {})
        migrated.setdefault("archived_close_intents", {})
        migrated.setdefault("protection_replacements", {})
        migrated.setdefault("archived_protection_replacements", {})
        migrated.setdefault("risk_session_utc_date", "")
        migrated["schema_version"] = 3
        version = 3

    if version != CURRENT_STATE_SCHEMA_VERSION:
        raise ValueError(f"state migration stopped at unsupported v{version}")
    return migrated


def _validate_state_dict(state):
    if not isinstance(state, dict):
        raise ValueError("state root must be a JSON object")
    for key in (
        "entry_metadata", "virtual_orders", "order_intents",
        "archived_order_intents", "close_intents", "archived_close_intents",
        "protection_replacements", "archived_protection_replacements",
        "trailing_trades", "short_high_confidence_counters",
    ):
        value = state.get(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"state.{key} must be an object")
    if not isinstance(state.get("active_symbols", []), list):
        raise ValueError("state.active_symbols must be an array")
    if int(state.get("schema_version", 0)) != CURRENT_STATE_SCHEMA_VERSION:
        raise ValueError("state schema version was not migrated completely")
    from core.config import Config
    if state.get("custom_risk_pct") is not None:
        risk_pct = float(state["custom_risk_pct"])
        if not (
            float(Config.MIN_RISK_PER_TRADE_PERCENT)
            <= risk_pct
            <= float(Config.HARD_MAX_RISK_PER_TRADE_PERCENT)
        ):
            raise ValueError(f"custom_risk_pct violates hard policy: {risk_pct}")
    if state.get("custom_leverage") is not None:
        leverage = int(state["custom_leverage"])
        if not (int(Config.MIN_LEVERAGE) <= leverage <= int(Config.MAX_LEVERAGE)):
            raise ValueError(f"custom_leverage violates hard policy: {leverage}")
    if state.get("max_open_trades") is not None:
        max_trades = int(state["max_open_trades"])
        if not (1 <= max_trades <= int(Config.HARD_MAX_OPEN_TRADES)):
            raise ValueError(f"max_open_trades violates hard policy: {max_trades}")
    if state.get("demo_signal_execution_mode") is not None:
        mode = str(state["demo_signal_execution_mode"]).strip().lower()
        if mode not in {"strict_proposal", "hybrid_decision"}:
            raise ValueError(f"invalid demo_signal_execution_mode: {mode}")
    return True


def _state_json_default(value):
    """Serialize supported scientific/date scalars without hiding unknown types."""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        encoded = isoformat()
        if isinstance(encoded, str):
            return encoded

    item = getattr(value, "item", None)
    if callable(item):
        encoded = item()
        if encoded is not value:
            return encoded

    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


class StateManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._dirty = False
        self._last_flush = 0.0
        self._FLUSH_MIN_INTERVAL = 0.5  # ثانية
        self._state_recovery_blocked = False
        self._state_recovery_source = "fresh"
        self._state_load_error = ""

        self.state = {
            "schema_version": CURRENT_STATE_SCHEMA_VERSION,
            "last_reset_date": "",
            "risk_session_utc_date": "",
            "initial_balance": 0.0,
            "active_symbols": [],
            "trailing_trades": {},
            "entry_metadata": {},
            "pair_locks": {},
            "setup_cooldowns": {},
            "consecutive_losses": {},
            "virtual_orders": {},
            "short_high_confidence_counters": {},
            "order_intents": {},
            "archived_order_intents": {},
            "close_intents": {},
            "archived_close_intents": {},
            "protection_replacements": {},
            "archived_protection_replacements": {},
            "recovery_complete": False,
            "last_successful_reconciliation_at": None,
            "last_scan_completed_at": None,
            "sl_guard_enabled": True,
            "sl_guard_trade_limit": 3,
            "sl_guard_pause_until": None,
            "max_drawdown_enabled": True,
            "max_drawdown_pct": 5.0,
            "max_drawdown_pause_until": None,
            "low_profit_enabled": True,
            "low_profit_loss_count": 2,
            "frozen_pairs": {}
        }
        # ضمان وجود مجلد الحفظ قبل أي محاولة قراءة/كتابة (مهم لبيئة Docker/Railway)
        state_dir = os.path.dirname(os.path.abspath(STATE_FILE))
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        self.load_state()
        # تنظيف أي علامات _closing عالقة من انهيارات/Crash سابقة
        self.cleanup_stale_closing_flags()

    def _normalize_sym(self, symbol: str) -> str:
        if not symbol:
            return ""
        sym = str(symbol).strip().upper().replace(" ", "")
        sym = sym.split(":")[0]
        if "/" in sym:
            return sym
        if sym.endswith("USDT"):
            return sym[:-4] + "/USDT"
        return f"{sym}/USDT"

    def get_state(self):
        with self._lock:
            return copy.deepcopy(self.state)

    def get(self, key, default=None):
        with self._lock:
            val = self.state.get(key, default)
            if isinstance(val, (dict, list)):
                return copy.deepcopy(val)
            return val

    def set(self, key, value):
        with self._lock:
            # Never retain a caller-owned mutable object.  Background shadow
            # monitors keep updating their private dictionaries after this
            # call; sharing that same object with ``self.state`` lets it change
            # while json.dump() is iterating and produces
            # ``dictionary changed size during iteration``.
            self.state[key] = copy.deepcopy(value)
            return self._save_state_unsafe()

    @staticmethod
    def _read_state_dict(path):
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if not isinstance(saved, dict):
            raise ValueError("state root must be a JSON object")
        return saved

    def load_state(self):
        """تحميل الحالة مع نسخة احتياطية ورفض صريح للحالة التالفة.

        بدء التداول بحالة افتراضية بعد تلف JSON قد يُسقط Kill Switch أو نوايا
        أوامر معلقة. لذلك لا نعتبر الملف التالف حالة جديدة، ونحاول آخر نسخة
        سليمة أولاً. إذا فشل الملفان تبقى الكتابة والتداول محجوبين حتى تدخل بشري.
        """
        if not os.path.exists(STATE_FILE):
            self._state_recovery_source = "fresh"
            return

        backup_path = f"{STATE_FILE}.bak"
        try:
            raw_saved = self._read_state_dict(STATE_FILE)
            saved = _migrate_state_dict(raw_saved)
            candidate = copy.deepcopy(self.state)
            candidate.update(saved)
            _validate_state_dict(candidate)
            with self._lock:
                self.state = candidate
            self._state_recovery_source = "primary"
            logger.info(
                f"تم تحميل الحالة المحفوظة. الصفقات المفتوحة: "
                f"{self.state.get('active_symbols', [])}"
            )
            if raw_saved.get("schema_version", 1) != CURRENT_STATE_SCHEMA_VERSION:
                logger.warning(
                    f"[StateMigration] ترقية ملف الحالة إلى schema v"
                    f"{CURRENT_STATE_SCHEMA_VERSION}."
                )
                if not self._save_state_unsafe(force=True):
                    self._state_recovery_blocked = True
                    self._state_load_error = "state migration persistence failed"
            return
        except FutureStateSchemaError as future_error:
            self._state_load_error = (
                f"primary={type(future_error).__name__}: {future_error}"
            )
            self._state_recovery_source = "future_schema_read_only"
            self._state_recovery_blocked = True
            logger.critical(
                "[StateRecovery] ملف الحالة من إصدار أحدث؛ لن تُقرأ النسخة "
                "الاحتياطية ولن يُكتب أي ملف بواسطة هذا الإصدار القديم."
            )
            return
        except Exception as primary_error:
            self._state_load_error = f"primary={type(primary_error).__name__}: {primary_error}"
            logger.critical(
                f"[StateRecovery] ملف الحالة الأساسي تالف؛ محاولة النسخة الاحتياطية: "
                f"{primary_error}"
            )

        if os.path.exists(backup_path):
            try:
                raw_saved = self._read_state_dict(backup_path)
                saved = _migrate_state_dict(raw_saved)
                candidate = copy.deepcopy(self.state)
                candidate.update(saved)
                _validate_state_dict(candidate)
                with self._lock:
                    self.state = candidate
                self._state_recovery_source = "backup"
                self._state_recovery_blocked = False
                logger.critical(
                    "[StateRecovery] استُعيدت آخر نسخة احتياطية سليمة؛ "
                    "سيُعاد بناء الملف الأساسي قبل السماح بالتداول."
                )
                if not self._save_state_unsafe(force=True):
                    self._state_recovery_blocked = True
                    self._state_load_error += "; backup restore persistence failed"
                return
            except Exception as backup_error:
                self._state_load_error += (
                    f"; backup={type(backup_error).__name__}: {backup_error}"
                )

        self._state_recovery_source = "unrecoverable"
        self._state_recovery_blocked = True
        logger.critical(
            "[StateRecovery] الملف الأساسي والنسخة الاحتياطية غير قابلين للقراءة. "
            "لن تُحفظ حالة افتراضية ولن يُسمح بصفقات جديدة قبل إصلاح الملفات."
        )

    def get_state_recovery_status(self):
        return {
            "blocked": bool(self._state_recovery_blocked),
            "source": self._state_recovery_source,
            "error": self._state_load_error,
            "state_file": STATE_FILE,
        }

    def is_state_recovery_blocked(self):
        return bool(self._state_recovery_blocked)

    @staticmethod
    def _write_bytes_atomically(path, payload):
        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=os.path.basename(path) + "_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _backup_current_state_file(self):
        """يحفظ فقط ملفاً أساسياً صالحاً، فلا تستبدل نسخة سليمة بملف تالف."""
        if not os.path.exists(STATE_FILE):
            return
        self._read_state_dict(STATE_FILE)
        with open(STATE_FILE, "rb") as handle:
            payload = handle.read()
        self._write_bytes_atomically(f"{STATE_FILE}.bak", payload)

    def _save_state_unsafe(self, force=False):
        """يحفظ الحالة بدون lock — يُستدعى فقط من داخل with self._lock."""
        if self._state_recovery_blocked:
            logger.critical(
                "[StateRecovery] رُفض حفظ حالة افتراضية فوق ملف تالف قبل التعافي."
            )
            return False
        now = time.monotonic()
        if not force and now - self._last_flush < self._FLUSH_MIN_INTERVAL:
            self._dirty = True
            return True  # نؤجل الكتابة
        tmp_path = None
        try:
            directory = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix="bot_state_", suffix=".tmp")

            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    self.state,
                    f,
                    indent=2,
                    default=_state_json_default,
                )
                f.flush()
                os.fsync(f.fileno())

            # النسخة الاحتياطية دائماً هي آخر ملف أساسي صالح، وليس الملف الجديد
            # نفسه. أي تلف أثناء/بعد replace يترك لنا نقطة رجوع مستقلة.
            if self._state_recovery_source != "backup":
                try:
                    self._backup_current_state_file()
                except Exception as backup_error:
                    logger.error(
                        f"[StateRecovery] تعذر تحديث النسخة الاحتياطية؛ رُفض حفظ الحالة: "
                        f"{backup_error}"
                    )
                    return False

            os.replace(tmp_path, STATE_FILE)
            if self._state_recovery_source == "backup":
                self._state_recovery_source = "primary_restored_from_backup"
            # مزامنة مدخل المجلد على POSIX تجعل rename متيناً أمام انقطاع مفاجئ.
            if hasattr(os, "O_DIRECTORY"):
                try:
                    dir_fd = os.open(directory, os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            self._dirty = False
            self._last_flush = now
            return True
        except Exception as e:
            logger.error(f"فشل حفظ ملف الحالة: {e}")
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def save_state(self, force=False):
        with self._lock:
            return self._save_state_unsafe(force=force)

    def flush_if_dirty(self):
        with self._lock:
            if self._dirty:
                return self._save_state_unsafe()
        return True

    # --- إدارة الرصيد اليومي ---
    def update_daily_balance(self, current_equity):
        """تحديث مرجع جلسة المخاطر اليومية عند حد اليوم UTC فقط."""
        if current_equity <= 0:
            logger.warning(f"[DailyBalance] تم تجاهل تحديث الرصيد المرجعي لأن القيمة صفر أو سالبة ({current_equity}).")
            return
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            session_day = str(
                self.state.get("risk_session_utc_date")
                or self.state.get("last_reset_date")
                or ""
            )
            if session_day != today:
                logger.info(
                    f"جلسة مخاطر UTC جديدة؛ تعيين الرصيد المرجعي إلى: "
                    f"{current_equity:.2f} USDT"
                )
                self.state["risk_session_utc_date"] = today
                self.state["last_reset_date"] = today
                self.state["initial_balance"] = current_equity
                # ── إعادة Kill Switch تلقائياً مع بداية اليوم ────────────
                if self.state.get("kill_switch_active", False):
                    self.state["kill_switch_active"] = False
                    logger.info("[KillSwitch] ✅ تم إلغاؤه تلقائياً مع بداية اليوم الجديد.")
                self._save_state_unsafe()
            elif not self.state.get("risk_session_utc_date"):
                # ترحيل legacy بلا إعادة ضبط الرصيد في منتصف الجلسة.
                self.state["risk_session_utc_date"] = today
                self._save_state_unsafe()


    def get_initial_balance(self):
        with self._lock:
            return float(self.state.get("initial_balance", 0.0))

    # --- إدارة الصفقات المفتوحة ---
    def save_active_symbols(self, symbols_set):
        with self._lock:
            normalized_set = {self._normalize_sym(s) for s in symbols_set if s}
            self.state["active_symbols"] = list(normalized_set)
            return self._save_state_unsafe()

    def get_active_symbols(self) -> set:
        with self._lock:
            symbols = self.state.get("active_symbols", [])
            normalized_set = {self._normalize_sym(s) for s in symbols if s}
            return normalized_set

    def add_active_symbol(self, symbol: str):
        """إضافة رمز بشكل ذري (آمن للخيوط المتوازية — يمنع Lost Update)"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            symbols = set(self.state.get("active_symbols", []))
            if symbol in symbols:
                return True
            symbols.add(symbol)
            self.state["active_symbols"] = list(symbols)
            return self._save_state_unsafe()

    def remove_active_symbol(self, symbol: str):
        """حذف رمز عملة واحدة من قائمة الصفقات المفتوحة بشكل ذري"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            symbols = set(self.state.get("active_symbols", []))
            symbols.discard(symbol)
            self.state["active_symbols"] = list(symbols)
            return self._save_state_unsafe()

    def cleanup_stale_closing_flags(self):
        """تنظيف أي علامات _closing عالقة بسبب Crash/إعادة نشر سابقة.
        تُستدعى تلقائياً عند كل إقلاع للبوت."""
        with self._lock:
            meta_dict = self.state.get("entry_metadata", {})
            changed = False
            for sym, meta in list(meta_dict.items()):
                if meta.get("_closing"):
                    logger.warning(
                        f"[StateManager] علامة _closing عالقة لـ {sym} من جلسة سابقة — تنظيف."
                    )
                    meta.pop("_closing", None)
                    changed = True
            if changed:
                self._save_state_unsafe()

    # --- إدارة الستوب المتحرك (Trailing Stop Persistence) ---
    def save_trailing_trades(self, trades_dict):
        """حفظ حالة الستوب المتحرك (أفضل سعر، الستوب الحالي) لكل صفقة مفتوحة"""
        with self._lock:
            normalized_dict = {}
            for k, v in trades_dict.items():
                normalized_dict[self._normalize_sym(k)] = v
            self.state["trailing_trades"] = normalized_dict
            return self._save_state_unsafe()

    def get_trailing_trades(self):
        """تحميل حالة الستوب المتحرك عند إعادة التشغيل بعد انقطاع"""
        with self._lock:
            return copy.deepcopy(self.state.get("trailing_trades", {}))

    def save_entry_metadata(self, symbol: str, metadata: dict):
        """حفظ مؤشرات وبيانات دخول الصفقة لاستردادها عند الإغلاق
        ملاحظة: يُستخدم force=True لأن بيانات الدخول حرجة ولا تتكرر كثيراً،
        ونريد ضمان الكتابة الفورية حتى لو صار crash بعد فتح الصفقة مباشرة."""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "entry_metadata" not in self.state:
                self.state["entry_metadata"] = {}
            self.state["entry_metadata"][symbol] = copy.deepcopy(metadata)
            # force=True: نتجاوز الـ debounce لضمان الحفظ الفوري على القرص
            self._last_flush = 0.0
            return self._save_state_unsafe()

    def get_entry_metadata(self, symbol: str) -> dict:
        """جلب بيانات دخول الصفقة بشكل آمن مع تجنب تداخل الذاكرة (Race Conditions)"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            return copy.deepcopy(
                self.state.get("entry_metadata", {}).get(symbol, {})
            )

    def pop_entry_metadata(self, symbol: str) -> dict:
        """استرداد وحذف بيانات دخول الصفقة بعد إغلاقها لتسجيلها في الملف المحلي"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "entry_metadata" not in self.state:
                return {}
            meta = self.state.get("entry_metadata", {}).pop(symbol, {})
            self._save_state_unsafe()
            return copy.deepcopy(meta)

    # --- إدارة أهداف جني الأرباح السُّلَّمي (TP Ladder) ---
    def get_tp_steps(self, symbol: str) -> list:
        """يجلب قائمة أهداف TP المتبقية للصفقة"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            meta = self.state.get("entry_metadata", {}).get(symbol, {})
            return copy.deepcopy(meta.get("tp_steps", []))

    def mark_tp_step_done(self, symbol: str, step_index: int, new_sl: float):
        """
        يحدّث حالة خطوة TP بعد تنفيذها:
        - يضع status='done' للخطوة المنفذة
        - يحدّث current_sl للحماية
        """
        symbol = self._normalize_sym(symbol)
        with self._lock:
            meta = self.state.get("entry_metadata", {}).get(symbol, {})
            steps = meta.get("tp_steps", [])
            if step_index < len(steps):
                steps[step_index]["status"] = "done"
            meta["tp_steps"]   = steps
            meta["current_sl"] = new_sl
            meta["step_trail_active"] = True
            meta["highest_tp_step_reached"] = max(
                int(meta.get("highest_tp_step_reached", 0) or 0),
                step_index + 1,
            )
            # تنظيف مفاتيح التصميم القديم؛ Step Trail يحتفظ بكامل الكمية.
            for legacy_key in (
                "partial_tp_enabled", "partial_tp_done", "partial_amount", "runner_amount"
            ):
                meta.pop(legacy_key, None)
            self.state["entry_metadata"][symbol] = meta
            self._save_state_unsafe()

    def get_sl_timeout_state(self, symbol: str) -> dict:
        """يجلب حالة مؤقت SL الزمني"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            meta = self.state.get("entry_metadata", {}).get(symbol, {})
            return copy.deepcopy(meta.get("sl_timeout", {}))

    def set_sl_timeout_state(self, symbol: str, timeout_data: dict):
        """يحفظ أو يمسح حالة مؤقت SL الزمني"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "entry_metadata" not in self.state:
                return
            meta = self.state.get("entry_metadata", {}).get(symbol, {})
            if timeout_data:
                meta["sl_timeout"] = timeout_data
            else:
                meta.pop("sl_timeout", None)
            self.state["entry_metadata"][symbol] = meta
            self._save_state_unsafe()

    def get_ttp_state(self, symbol: str) -> dict:
        """يجلب حالة Trailing Take Profit (أعلى/أدنى سعر بعد تفعيل TTP)"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            meta = self.state.get("entry_metadata", {}).get(symbol, {})
            return copy.deepcopy(meta.get("ttp_state", {}))

    def set_ttp_state(self, symbol: str, ttp_data: dict):
        """يحفظ أو يمسح حالة TTP"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "entry_metadata" not in self.state:
                return
            meta = self.state.get("entry_metadata", {}).get(symbol, {})
            if ttp_data:
                meta["ttp_state"] = ttp_data
            else:
                meta.pop("ttp_state", None)
            self.state["entry_metadata"][symbol] = meta
            self._save_state_unsafe()

    def increment_rejection_counter(self, filter_name: str):
        """زيادة عداد الفلاتر المرفوضة (مثل Anti-Pump, Spread, Margin)"""
        with self._lock:
            if "filter_rejections" not in self.state:
                self.state["filter_rejections"] = {}
            self.state["filter_rejections"][filter_name] = self.state["filter_rejections"].get(filter_name, 0) + 1
            self._save_state_unsafe()

    def get_rejection_counters(self) -> dict:
        """الحصول على عدادات التصفية والفلاتر المرفوضة الحالية"""
        with self._lock:
            return copy.deepcopy(self.state.get("filter_rejections", {}))

    def reset_rejection_counters(self):
        """تصفير عدادات الفلاتر للبدء من جديد (يومياً)"""
        with self._lock:
            self.state["filter_rejections"] = {}
            self._save_state_unsafe()

    # --- إدارة تبريد العملات (PairLocks) ---
    def record_trade_result(self, symbol: str, is_profit: bool):
        """تسجيل نتيجة الصفقة لمعرفة الخسائر المتتالية"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "consecutive_losses" not in self.state:
                self.state["consecutive_losses"] = {}
                
            if is_profit:
                self.state["consecutive_losses"][symbol] = 0
            else:
                self.state["consecutive_losses"][symbol] = self.state["consecutive_losses"].get(symbol, 0) + 1
                
            self._save_state_unsafe()
            return self.state["consecutive_losses"][symbol]

    def lock_pair(self, symbol: str, lock_until_timestamp: float, reason: str):
        """قفل العملة ومنع التداول عليها حتى وقت محدد"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "pair_locks" not in self.state:
                self.state["pair_locks"] = {}
            self.state["pair_locks"][symbol] = {
                "lock_until": lock_until_timestamp,
                "reason": reason
            }
            self._save_state_unsafe()

    def is_pair_locked(self, symbol: str) -> bool:
        """التحقق مما إذا كانت العملة مقفولة حالياً"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            locks = self.state.get("pair_locks", {})
            if symbol in locks:
                lock_until = locks[symbol].get("lock_until", 0)
                if datetime.now().timestamp() < lock_until:
                    return True
                else:
                    # انتهى الحظر، نقوم بالتنظيف
                    del self.state["pair_locks"][symbol]
                    # تصفير عداد الخسائر ليعطى فرصة جديدة
                    if "consecutive_losses" in self.state and symbol in self.state["consecutive_losses"]:
                        self.state["consecutive_losses"][symbol] = 0
                    self._save_state_unsafe()
            return False
    
    def get_pair_lock_info(self, symbol: str) -> dict:
        symbol = self._normalize_sym(symbol)
        with self._lock:
            return copy.deepcopy(self.state.get("pair_locks", {}).get(symbol, {}))

    # --- منع إعادة الفكرة/الهيكل نفسه بعد إغلاقها أو إبطالها ---
    def set_setup_cooldown(
        self,
        symbol: str,
        thesis_id: str,
        duration_minutes: float,
        reason: str,
        details: dict = None,
    ) -> bool:
        symbol = self._normalize_sym(symbol)
        thesis_id = str(thesis_id or "").strip()
        if not symbol or not thesis_id:
            return False
        now = time.time()
        record = {
            "thesis_id": thesis_id,
            "created_at": now,
            "expires_at": now + max(1.0, float(duration_minutes)) * 60.0,
            "reason": str(reason or "SETUP_COOLDOWN"),
            "details": copy.deepcopy(details or {}),
        }
        with self._lock:
            by_symbol = self.state.setdefault("setup_cooldowns", {}).setdefault(symbol, {})
            previous = copy.deepcopy(by_symbol.get(thesis_id))
            by_symbol[thesis_id] = record
            if self._save_state_unsafe(force=True):
                return True
            if previous is None:
                by_symbol.pop(thesis_id, None)
            else:
                by_symbol[thesis_id] = previous
            return False

    def get_setup_cooldown(self, symbol: str, thesis_id: str) -> dict:
        """Return an active lock for the same thesis; a new structure is free."""
        symbol = self._normalize_sym(symbol)
        thesis_id = str(thesis_id or "").strip()
        if not symbol or not thesis_id:
            return {}
        now = time.time()
        with self._lock:
            all_locks = self.state.setdefault("setup_cooldowns", {})
            by_symbol = all_locks.get(symbol, {})
            record = by_symbol.get(thesis_id)
            if not isinstance(record, dict):
                return {}
            if float(record.get("expires_at", 0) or 0) > now:
                return copy.deepcopy(record)
            by_symbol.pop(thesis_id, None)
            if not by_symbol:
                all_locks.pop(symbol, None)
            self._save_state_unsafe()
            return {}

    def cleanup_setup_cooldowns(self) -> int:
        now = time.time()
        removed = 0
        with self._lock:
            all_locks = self.state.setdefault("setup_cooldowns", {})
            for symbol, by_symbol in list(all_locks.items()):
                if not isinstance(by_symbol, dict):
                    all_locks.pop(symbol, None)
                    removed += 1
                    continue
                for thesis_id, record in list(by_symbol.items()):
                    expires_at = float((record or {}).get("expires_at", 0) or 0)
                    if expires_at <= now:
                        by_symbol.pop(thesis_id, None)
                        removed += 1
                if not by_symbol:
                    all_locks.pop(symbol, None)
            if removed:
                self._save_state_unsafe()
        return removed

    # --- إدارة الأوامر المعلقة الافتراضية (Virtual Pending Orders / Sniper) ---
    def save_virtual_order(self, symbol: str, order_data: dict):
        """حفظ أمر معلق افتراضي لانتظار السعر المثالي"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            orders = self.state.setdefault("virtual_orders", {})
            had_previous = symbol in orders
            previous = copy.deepcopy(orders.get(symbol))
            orders[symbol] = copy.deepcopy(order_data)
            if self._save_state_unsafe(force=True):
                return True
            if had_previous:
                orders[symbol] = previous
            else:
                orders.pop(symbol, None)
            return False

    def get_virtual_orders(self) -> dict:
        """جلب جميع الأوامر المعلقة"""
        with self._lock:
            return copy.deepcopy(self.state.get("virtual_orders", {}))

    def remove_virtual_order(self, symbol: str):
        """حذف أمر معلق افتراضي (إما تم تنفيذه أو انتهت صلاحيته)"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            orders = self.state.setdefault("virtual_orders", {})
            if symbol not in orders:
                return True
            previous = copy.deepcopy(orders[symbol])
            del orders[symbol]
            if self._save_state_unsafe(force=True):
                return True
            orders[symbol] = previous
            return False

    def increment_short_high_confidence_counter(self, counter: str) -> int:
        """Durably increment one of the SHORT_HIGH_CONFIDENCE_V1 counters."""
        key = str(counter or "").strip().upper()
        if not key:
            return 0
        with self._lock:
            counters = self.state.setdefault("short_high_confidence_counters", {})
            previous = int(counters.get(key, 0) or 0)
            counters[key] = previous + 1
            if self._save_state_unsafe(force=True):
                return counters[key]
            counters[key] = previous
            return previous

    def get_short_high_confidence_counters(self) -> dict:
        with self._lock:
            return copy.deepcopy(
                self.state.get("short_high_confidence_counters", {}) or {}
            )

    # --- نوايا أوامر الدخول (Idempotent Order Intents) ---
    def save_order_intent(self, symbol: str, intent_data: dict):
        symbol = self._normalize_sym(symbol)
        with self._lock:
            intents = self.state.setdefault("order_intents", {})
            had_previous = symbol in intents
            previous = copy.deepcopy(intents.get(symbol))
            intents[symbol] = copy.deepcopy(intent_data)
            # نية الأمر حاجز أمان قبل Binance؛ لا يجوز تأجيلها بواسطة debounce.
            if self._save_state_unsafe(force=True):
                return True
            if had_previous:
                intents[symbol] = previous
            else:
                intents.pop(symbol, None)
            return False

    def get_order_intent(self, symbol: str) -> dict:
        symbol = self._normalize_sym(symbol)
        with self._lock:
            return copy.deepcopy(self.state.get("order_intents", {}).get(symbol, {}))

    def get_order_intents(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.state.get("order_intents", {}))

    def transition_order_intent(
        self,
        symbol: str,
        expected_statuses,
        new_status: str,
        updates: dict = None,
    ) -> bool:
        """انتقال ذري ودائم لحالة نية أمر الدخول.

        يمنع خيط الاسترداد من الكتابة فوق مرحلة أحدث أنجزها خيط التنفيذ.
        ``expected_statuses`` يمكن أن يكون نصاً واحداً أو مجموعة حالات، وNone
        يعني قبول الحالة الحالية أياً كانت.
        """
        symbol = self._normalize_sym(symbol)
        with self._lock:
            intents = self.state.setdefault("order_intents", {})
            current = intents.get(symbol)
            if not isinstance(current, dict) or not current:
                return False

            if expected_statuses is not None:
                allowed = (
                    {str(expected_statuses)}
                    if isinstance(expected_statuses, str)
                    else {str(value) for value in expected_statuses}
                )
                if str(current.get("status", "")) not in allowed:
                    return False

            previous = copy.deepcopy(current)
            current = copy.deepcopy(current)
            if updates:
                current.update(copy.deepcopy(updates))
            current["status"] = str(new_status)
            current["updated_at"] = time.time()
            intents[symbol] = current

            if self._save_state_unsafe(force=True):
                return True

            intents[symbol] = previous
            return False

    def remove_order_intent(self, symbol: str):
        symbol = self._normalize_sym(symbol)
        with self._lock:
            intents = self.state.setdefault("order_intents", {})
            had_previous = symbol in intents
            previous = copy.deepcopy(intents.get(symbol))
            intents.pop(symbol, None)
            if self._save_state_unsafe(force=True):
                return True
            if had_previous:
                intents[symbol] = previous
            return False

    def archive_order_intent(self, symbol: str, terminal_status: str, proofs=None):
        """Move a terminal intent to a bounded durable audit archive.

        Callers must supply a definitive terminal status.  UNKNOWN/absent is
        deliberately rejected so an API timeout can never erase ownership.
        """
        terminal_status = str(terminal_status or "").upper()
        if terminal_status in {"", "UNKNOWN", "NOT_FOUND", "CLOSED_POSITION_ONLY"}:
            return False
        symbol = self._normalize_sym(symbol)
        with self._lock:
            intents = self.state.setdefault("order_intents", {})
            current = intents.get(symbol)
            if not isinstance(current, dict) or not current:
                return False
            archived = self.state.setdefault("archived_order_intents", {})
            previous_archive = copy.deepcopy(archived.get(symbol))
            archived[symbol] = {
                **copy.deepcopy(current),
                "status": terminal_status,
                "terminal_proofs": copy.deepcopy(proofs or {}),
                "archived_at": time.time(),
            }
            intents.pop(symbol, None)
            if self._save_state_unsafe(force=True):
                # The active bot only needs a small forensic tail; SQLite and
                # decision_audit remain the long-term event ledger.
                if len(archived) > 200:
                    oldest = sorted(
                        archived,
                        key=lambda key: float(archived[key].get("archived_at", 0)),
                    )[:-200]
                    for key in oldest:
                        archived.pop(key, None)
                    self._save_state_unsafe(force=True)
                return True
            intents[symbol] = current
            if previous_archive is None:
                archived.pop(symbol, None)
            else:
                archived[symbol] = previous_archive
            return False

    # --- نوايا إغلاق المراكز (Idempotent Close Sagas) ---
    def _position_key(self, symbol: str, position_side: str) -> str:
        return f"{self._normalize_sym(symbol)}|{str(position_side or 'BOTH').upper()}"

    def get_close_intent(self, symbol: str, position_side: str) -> dict:
        key = self._position_key(symbol, position_side)
        with self._lock:
            return copy.deepcopy(self.state.get("close_intents", {}).get(key, {}))

    def prepare_close_intent(
        self,
        symbol: str,
        position_side: str,
        side: str,
        amount: float,
        position_id=None,
        reason=None,
    ) -> dict:
        """Persist one close generation before any command reaches Binance."""
        key = self._position_key(symbol, position_side)
        with self._lock:
            intents = self.state.setdefault("close_intents", {})
            existing = intents.get(key)
            if isinstance(existing, dict) and existing:
                return copy.deepcopy(existing)

            archived = self.state.setdefault("archived_close_intents", {})
            prior_generations = [
                int(value.get("generation") or 0)
                for value in archived.values()
                if isinstance(value, dict) and value.get("position_key") == key
            ]
            generation = max(prior_generations or [0]) + 1
            safe_side = "L" if str(position_side).upper() == "LONG" else "S"
            client_id = f"cb_{safe_side}_{uuid.uuid4().hex[:28]}"[:36]
            now = time.time()
            intent = {
                "position_key": key,
                "symbol": self._normalize_sym(symbol),
                "position_side": str(position_side or "BOTH").upper(),
                "side": str(side or "").upper(),
                "amount": float(amount or 0),
                "position_id": position_id,
                "reason": reason,
                "generation": generation,
                "client_order_id": client_id,
                "status": "PREPARED",
                "created_at": now,
                "updated_at": now,
            }
            intents[key] = intent
            if self._save_state_unsafe(force=True):
                return copy.deepcopy(intent)
            intents.pop(key, None)
            return {}

    def transition_close_intent(
        self,
        symbol: str,
        position_side: str,
        expected_statuses,
        new_status: str,
        updates=None,
    ) -> bool:
        key = self._position_key(symbol, position_side)
        with self._lock:
            intents = self.state.setdefault("close_intents", {})
            current = intents.get(key)
            if not isinstance(current, dict) or not current:
                return False
            if expected_statuses is not None:
                allowed = (
                    {str(expected_statuses)}
                    if isinstance(expected_statuses, str)
                    else {str(value) for value in expected_statuses}
                )
                if str(current.get("status")) not in allowed:
                    return False
            previous = copy.deepcopy(current)
            current = copy.deepcopy(current)
            if updates:
                current.update(copy.deepcopy(updates))
            current["status"] = str(new_status)
            current["updated_at"] = time.time()
            intents[key] = current
            if self._save_state_unsafe(force=True):
                return True
            intents[key] = previous
            return False

    def archive_close_intent(
        self, symbol: str, position_side: str, terminal_status: str, proofs=None
    ) -> bool:
        terminal_status = str(terminal_status or "").upper()
        if terminal_status in {"", "UNKNOWN"}:
            return False
        key = self._position_key(symbol, position_side)
        with self._lock:
            intents = self.state.setdefault("close_intents", {})
            current = intents.get(key)
            if not isinstance(current, dict) or not current:
                return False
            archive = self.state.setdefault("archived_close_intents", {})
            archive_key = f"{key}#{int(current.get('generation') or 0)}"
            previous_archive = copy.deepcopy(archive.get(archive_key))
            archive[archive_key] = {
                **copy.deepcopy(current),
                "status": terminal_status,
                "terminal_proofs": copy.deepcopy(proofs or {}),
                "archived_at": time.time(),
            }
            intents.pop(key, None)
            if self._save_state_unsafe(force=True):
                return True
            intents[key] = current
            if previous_archive is None:
                archive.pop(archive_key, None)
            else:
                archive[archive_key] = previous_archive
            return False

    # --- استبدال وقف الخسارة (Durable Protection Replacement Saga) ---
    def get_protection_replacement(self, symbol: str, position_side: str) -> dict:
        key = self._position_key(symbol, position_side)
        with self._lock:
            return copy.deepcopy(
                self.state.get("protection_replacements", {}).get(key, {})
            )

    def get_protection_replacements(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.state.get("protection_replacements", {}))

    def prepare_protection_replacement(
        self,
        symbol: str,
        position_side: str,
        side: str,
        amount: float,
        old_orders: list,
        old_stop: float,
        new_stop: float,
    ) -> dict:
        """Persist the complete replacement plan before the first cancel call."""
        key = self._position_key(symbol, position_side)
        with self._lock:
            active = self.state.setdefault("protection_replacements", {})
            existing = active.get(key)
            if isinstance(existing, dict) and existing:
                return copy.deepcopy(existing)
            archive = self.state.setdefault(
                "archived_protection_replacements", {}
            )
            prior_generations = [
                int(value.get("generation") or 0)
                for value in archive.values()
                if isinstance(value, dict) and value.get("position_key") == key
            ]
            generation = max(prior_generations or [0]) + 1
            token = uuid.uuid4().hex[:24]
            now = time.time()
            intent = {
                "position_key": key,
                "symbol": self._normalize_sym(symbol),
                "position_side": str(position_side or "BOTH").upper(),
                "side": str(side or "").upper(),
                "amount": float(amount or 0.0),
                "old_orders": copy.deepcopy(old_orders or []),
                "old_stop": float(old_stop or 0.0),
                "new_stop": float(new_stop or 0.0),
                "new_client_order_id": f"slnew_{token}"[:36],
                "restore_client_order_id": f"slold_{token}"[:36],
                "generation": generation,
                "phase": "PREPARED",
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
            }
            active[key] = intent
            if self._save_state_unsafe(force=True):
                return copy.deepcopy(intent)
            active.pop(key, None)
            return {}

    def transition_protection_replacement(
        self,
        symbol: str,
        position_side: str,
        expected_phases,
        new_phase: str,
        updates=None,
    ) -> bool:
        key = self._position_key(symbol, position_side)
        with self._lock:
            active = self.state.setdefault("protection_replacements", {})
            current = active.get(key)
            if not isinstance(current, dict) or not current:
                return False
            if expected_phases is not None:
                allowed = (
                    {str(expected_phases)}
                    if isinstance(expected_phases, str)
                    else {str(value) for value in expected_phases}
                )
                if str(current.get("phase")) not in allowed:
                    return False
            previous = copy.deepcopy(current)
            current = copy.deepcopy(current)
            if updates:
                current.update(copy.deepcopy(updates))
            current["phase"] = str(new_phase)
            current["updated_at"] = time.time()
            current["attempts"] = int(current.get("attempts") or 0) + 1
            active[key] = current
            if self._save_state_unsafe(force=True):
                return True
            active[key] = previous
            return False

    def archive_protection_replacement(
        self, symbol: str, position_side: str, terminal_phase: str, proofs=None
    ) -> bool:
        terminal_phase = str(terminal_phase or "").upper()
        if terminal_phase in {"", "UNKNOWN", "PREPARED", "CANCEL_IN_PROGRESS"}:
            return False
        key = self._position_key(symbol, position_side)
        with self._lock:
            active = self.state.setdefault("protection_replacements", {})
            current = active.get(key)
            if not isinstance(current, dict) or not current:
                return False
            archive = self.state.setdefault(
                "archived_protection_replacements", {}
            )
            archive_key = f"{key}#{int(current.get('generation') or 0)}"
            previous_archive = copy.deepcopy(archive.get(archive_key))
            archive[archive_key] = {
                **copy.deepcopy(current),
                "phase": terminal_phase,
                "terminal_proofs": copy.deepcopy(proofs or {}),
                "archived_at": time.time(),
            }
            active.pop(key, None)
            if self._save_state_unsafe(force=True):
                if len(archive) > 500:
                    oldest = sorted(
                        archive,
                        key=lambda item: float(archive[item].get("archived_at", 0)),
                    )[:-500]
                    for item in oldest:
                        archive.pop(item, None)
                    self._save_state_unsafe(force=True)
                return True
            active[key] = current
            if previous_archive is None:
                archive.pop(archive_key, None)
            else:
                archive[archive_key] = previous_archive
            return False
    # --- إدارة cooldown إشارات OBV ---
    def set_obv_signal_time(self, symbol: str, timestamp: float):
        """حفظ وقت آخر إشارة OBV BUY لمنع التكرار السريع"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "obv_last_signal" not in self.state:
                self.state["obv_last_signal"] = {}
            self.state["obv_last_signal"][symbol] = timestamp
            self._save_state_unsafe()

    def get_obv_signal_time(self, symbol: str) -> float:
        """جلب وقت آخر إشارة OBV BUY"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            return float(self.state.get("obv_last_signal", {}).get(symbol, 0.0))

    # --- إدارة نسبة المخاطرة الديناميكية ---
    def set_risk_pct(self, pct: float):
        """حفظ نسبة المخاطرة المخصصة (تتجاوز Config عند تعيينها)"""
        from core.config import Config
        value = float(pct)
        if not (
            float(Config.MIN_RISK_PER_TRADE_PERCENT)
            <= value
            <= float(Config.HARD_MAX_RISK_PER_TRADE_PERCENT)
        ):
            logger.warning(
                f"[RiskConfig] رُفضت مخاطرة {value}% خارج الحد الصلب "
                f"{Config.MIN_RISK_PER_TRADE_PERCENT}-"
                f"{Config.HARD_MAX_RISK_PER_TRADE_PERCENT}%."
            )
            return False
        with self._lock:
            previous = self.state.get("custom_risk_pct")
            self.state["custom_risk_pct"] = value
            if self._save_state_unsafe(force=True):
                return True
            if previous is None:
                self.state.pop("custom_risk_pct", None)
            else:
                self.state["custom_risk_pct"] = previous
            return False

    def get_risk_pct(self) -> float:
        """جلب نسبة المخاطرة: المخصصة من تيليجرام إن وجدت، وإلا القيمة الافتراضية من Config"""
        with self._lock:
            custom = self.state.get("custom_risk_pct", None)
            if custom is not None:
                return float(custom)
            from core.config import Config
            return float(getattr(Config, "RISK_PER_TRADE_PERCENT", 1.0))

    # --- إدارة الرافعة المالية الديناميكية ---
    def set_leverage(self, leverage: int):
        """حفظ الرافعة المالية المخصصة (تتجاوز Config عند تعيينها)"""
        from core.config import Config
        value = int(leverage)
        if not (int(Config.MIN_LEVERAGE) <= value <= int(Config.MAX_LEVERAGE)):
            logger.warning(
                f"[RiskConfig] رُفضت رافعة {value}x خارج الحد الصلب "
                f"{Config.MIN_LEVERAGE}-{Config.MAX_LEVERAGE}x."
            )
            return False
        with self._lock:
            previous = self.state.get("custom_leverage")
            self.state["custom_leverage"] = value
            if self._save_state_unsafe(force=True):
                return True
            if previous is None:
                self.state.pop("custom_leverage", None)
            else:
                self.state["custom_leverage"] = previous
            return False

    def get_leverage(self) -> int:
        """جلب الرافعة: المخصصة من تيليجرام إن وجدت، وإلا القيمة الافتراضية من Config"""
        with self._lock:
            custom = self.state.get("custom_leverage", None)
            if custom is not None:
                return int(custom)
            from core.config import Config
            return int(getattr(Config, "LEVERAGE", 5))

    def set_max_open_trades(self, value: int):
        """حفظ الحد الأقصى بشكل دائم كي لا يضيع بعد Railway restart."""
        from core.config import Config
        value = int(value)
        if not (1 <= value <= int(Config.HARD_MAX_OPEN_TRADES)):
            logger.warning(
                f"[RiskConfig] رُفض max_open_trades={value}; الحد الصلب "
                f"1-{Config.HARD_MAX_OPEN_TRADES}."
            )
            return False
        with self._lock:
            previous = self.state.get("max_open_trades")
            self.state["max_open_trades"] = value
            if self._save_state_unsafe(force=True):
                return True
            if previous is None:
                self.state.pop("max_open_trades", None)
            else:
                self.state["max_open_trades"] = previous
            return False

    def get_max_open_trades(self) -> int:
        with self._lock:
            saved = self.state.get("max_open_trades")
            if saved is not None:
                return max(1, int(saved))
            from core.config import Config
            default = (
                getattr(Config, "TESTNET_MAX_OPEN_TRADES", Config.MAX_OPEN_TRADES)
                if getattr(Config, "USE_TESTNET", False)
                else Config.MAX_OPEN_TRADES
            )
            return max(1, int(default))

    def set_demo_signal_execution_mode(self, mode: str) -> bool:
        """Persist which Telegram signal stage may reach Demo execution."""
        mode = str(mode or "").strip().lower()
        if mode not in {"strict_proposal", "hybrid_decision"}:
            logger.warning(f"[DemoSignalMode] رُفض وضع غير معروف: {mode}")
            return False
        with self._lock:
            previous = self.state.get("demo_signal_execution_mode")
            self.state["demo_signal_execution_mode"] = mode
            if self._save_state_unsafe(force=True):
                return True
            if previous is None:
                self.state.pop("demo_signal_execution_mode", None)
            else:
                self.state["demo_signal_execution_mode"] = previous
            return False

    def get_demo_signal_execution_mode(self) -> str:
        """Return the persisted mode; new Demo installs default to HYBRID."""
        with self._lock:
            mode = str(
                self.state.get("demo_signal_execution_mode")
                or "hybrid_decision"
            ).strip().lower()
            if mode not in {"strict_proposal", "hybrid_decision"}:
                return "strict_proposal"
            return mode

    # --- إدارة القائمة السوداء الدائمة (Blacklist) ---
    def blacklist_pair(self, symbol: str):
        """إضافة عملة للقائمة السوداء الدائمة (مختلفة عن freeze المؤقت)"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "blacklisted_pairs" not in self.state:
                self.state["blacklisted_pairs"] = []
            if symbol not in self.state["blacklisted_pairs"]:
                self.state["blacklisted_pairs"].append(symbol)
                self._save_state_unsafe()

    def whitelist_pair(self, symbol: str):
        """إزالة عملة من القائمة السوداء"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            bl = self.state.get("blacklisted_pairs", [])
            if symbol in bl:
                bl.remove(symbol)
                self.state["blacklisted_pairs"] = bl
                self._save_state_unsafe()

    def is_blacklisted(self, symbol: str) -> bool:
        """هل العملة في القائمة السوداء؟"""
        symbol = self._normalize_sym(symbol)
        with self._lock:
            return symbol in self.state.get("blacklisted_pairs", [])

    def get_blacklisted_pairs(self) -> list:
        """جلب كل العملات في القائمة السوداء"""
        with self._lock:
            return list(self.state.get("blacklisted_pairs", []))

    # --- StoplossGuard ---
    def get_sl_guard_enabled(self) -> bool:
        with self._lock:
            val = self.state.get("sl_guard_enabled", None)
            if val is None:
                from core.config import Config
                return bool(getattr(Config, "SL_GUARD_ENABLED", True))
            return bool(val)

    def set_sl_guard_enabled(self, enabled: bool):
        with self._lock:
            self.state["sl_guard_enabled"] = bool(enabled)
            self._save_state_unsafe()

    def get_sl_guard_limit(self) -> int:
        with self._lock:
            return int(self.state.get("sl_guard_trade_limit", 3))

    def set_sl_guard_limit(self, limit: int):
        with self._lock:
            self.state["sl_guard_trade_limit"] = int(limit)
            self._save_state_unsafe()

    def get_sl_guard_pause(self):
        with self._lock:
            val = self.state.get("sl_guard_pause_until")
            if val:
                try:
                    return datetime.fromisoformat(val)
                except (ValueError, TypeError):
                    logger.warning(f"[StateManager] قيمة sl_guard_pause_until غير صالحة: {val}")
                    self.state["sl_guard_pause_until"] = None
            return None

    def set_sl_guard_pause(self, dt):
        with self._lock:
            self.state["sl_guard_pause_until"] = dt.isoformat() if dt else None
            self._save_state_unsafe()

    def clear_sl_guard_pause(self):
        self.set_sl_guard_pause(None)

    # --- MaxDrawdown ---
    def get_max_drawdown_enabled(self) -> bool:
        with self._lock:
            val = self.state.get("max_drawdown_enabled", None)
            if val is None:
                from core.config import Config
                return bool(getattr(Config, "MAX_DRAWDOWN_ENABLED", True))
            return bool(val)

    def set_max_drawdown_enabled(self, enabled: bool):
        with self._lock:
            self.state["max_drawdown_enabled"] = bool(enabled)
            self._save_state_unsafe()

    def get_max_drawdown_pct(self) -> float:
        with self._lock:
            return float(self.state.get("max_drawdown_pct", 5.0))

    def set_max_drawdown_pct(self, pct: float):
        with self._lock:
            self.state["max_drawdown_pct"] = float(pct)
            self._save_state_unsafe()

    def get_max_drawdown_pause(self):
        with self._lock:
            val = self.state.get("max_drawdown_pause_until")
            if val:
                try:
                    return datetime.fromisoformat(val)
                except (ValueError, TypeError):
                    logger.warning(f"[StateManager] قيمة max_drawdown_pause_until غير صالحة: {val}")
                    self.state["max_drawdown_pause_until"] = None
            return None

    def set_max_drawdown_pause(self, dt):
        with self._lock:
            self.state["max_drawdown_pause_until"] = dt.isoformat() if dt else None
            self._save_state_unsafe()

    def clear_max_drawdown_pause(self):
        self.set_max_drawdown_pause(None)

    # --- LowProfitPairs ---
    def get_low_profit_enabled(self) -> bool:
        with self._lock:
            val = self.state.get("low_profit_enabled", None)
            if val is None:
                from core.config import Config
                return bool(getattr(Config, "LOW_PROFIT_ENABLED", True))
            return bool(val)

    def set_low_profit_enabled(self, enabled: bool):
        with self._lock:
            self.state["low_profit_enabled"] = bool(enabled)
            self._save_state_unsafe()

    def get_low_profit_loss_count(self) -> int:
        with self._lock:
            return int(self.state.get("low_profit_loss_count", 2))

    def set_low_profit_loss_count(self, count: int):
        with self._lock:
            self.state["low_profit_loss_count"] = int(count)
            self._save_state_unsafe()

    def freeze_pair(self, symbol: str, hours: float):
        symbol = self._normalize_sym(symbol)
        with self._lock:
            if "frozen_pairs" not in self.state:
                self.state["frozen_pairs"] = {}
            until = (datetime.now() + timedelta(hours=hours)).isoformat()
            self.state["frozen_pairs"][symbol] = until
            self._save_state_unsafe()

    def is_pair_frozen(self, symbol: str) -> bool:
        symbol = self._normalize_sym(symbol)
        with self._lock:
            frozen = self.state.get("frozen_pairs", {})
            if symbol in frozen:
                try:
                    until = datetime.fromisoformat(frozen[symbol])
                except (ValueError, TypeError):
                    logger.error(f"[StateManager] تنسيق تاريخ فاسد لـ {symbol}، سيتم فك التجميد فوراً.")
                    del self.state["frozen_pairs"][symbol]
                    self._save_state_unsafe()
                    return False

                if datetime.now() < until:
                    return True
                else:
                    del self.state["frozen_pairs"][symbol]
                    if "consecutive_losses" in self.state and symbol in self.state["consecutive_losses"]:
                        self.state["consecutive_losses"][symbol] = 0
                    self._save_state_unsafe()
            return False

    def get_frozen_pairs(self) -> dict:
        with self._lock:
            frozen = self.state.get("frozen_pairs", {})
            now = datetime.now()
            active = {}

            for k, v in list(frozen.items()):
                try:
                    if now < datetime.fromisoformat(v):
                        active[self._normalize_sym(k)] = v
                except (ValueError, TypeError):
                    logger.warning(f"[StateManager] إزالة تاريخ فاسد للزوج {k} أثناء جلب القائمة.")
                    del self.state["frozen_pairs"][k]
                    self._dirty = True

            if self._dirty or len(active) != len(frozen):
                self.state["frozen_pairs"] = active
                self._save_state_unsafe()

            return copy.deepcopy(active)

    def unfreeze_all_pairs(self):
        with self._lock:
            self.state["frozen_pairs"] = {}
            self._save_state_unsafe()
