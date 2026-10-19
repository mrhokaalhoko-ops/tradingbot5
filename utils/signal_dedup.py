import threading
from core.logger import logger

# ── Lock مستقل — لا نعتمد على Lock الـ state_manager الداخلي ────
_dedup_lock = threading.Lock()

# ── المفتاح داخل state_manager JSON ─────────────────────────────
_STATE_KEY = "signal_dedup_map"


class SignalDeduplicator:
    """
    يمنع تكرار الدخول على نفس الشمعة حتى عند إعادة تشغيل البوت.

    الحالة تُحفظ في state_manager بالشكل:
        {
            "signal_dedup_map": {
                "BTC/USDT": 1750298400000,
                "ETH/USDT": 1750298400000
            }
        }
    """

    def __init__(self, state_manager):
        """
        state_manager: كائن StateManager الذي يدعم get() و set()
        """
        self._sm = state_manager

    # ─────────────────────────────────────────────────────────────
    # القراءة والكتابة الداخلية
    # ─────────────────────────────────────────────────────────────

    def _get_map(self) -> dict:
        """يقرأ الـ map الكامل من state_manager"""
        return self._sm.get(_STATE_KEY, {})

    def _save_map(self, dedup_map: dict):
        """يحفظ الـ map الكامل في state_manager (Atomic Write)"""
        self._sm.set(_STATE_KEY, dedup_map)

    # ─────────────────────────────────────────────────────────────
    # API العامة
    # ─────────────────────────────────────────────────────────────

    def is_duplicate(self, symbol: str, bar_open_time_ms: int) -> bool:
        """
        يتحقق إذا كانت الإشارة مكررة.

        Parameters:
            symbol           : رمز العملة (مثال: "BTC/USDT")
            bar_open_time_ms : وقت فتح الشمعة بالـ ms من CCXT (ohlcv[-2][0])
                               نستخدم [-2] (الشمعة المغلقة) لا [-1] (الشمعة الحية)

        Returns:
            True  → إشارة مكررة، تجاهلها
            False → إشارة جديدة، يمكن معالجتها
        """
        with _dedup_lock:
            dedup_map = self._get_map()
            last_bar  = dedup_map.get(symbol)

        if last_bar == bar_open_time_ms:
            logger.debug(
                f"[Dedup] ⛔ إشارة مكررة — {symbol} "
                f"| bar_time={bar_open_time_ms} "
                f"| تجاهل"
            )
            return True

        return False

    def register_signal(self, symbol: str, bar_open_time_ms: int):
        """
        يُسجّل الإشارة بعد تأكيد فتح الصفقة فعلاً.

        ⚠️ مهم جداً: ادعُ هذه الدالة فقط بعد نجاح place_order
           وليس قبله — لتجنب منع إعادة المحاولة في حال فشل الأمر.

        Parameters:
            symbol           : رمز العملة
            bar_open_time_ms : نفس الوقت المُمرَّر لـ is_duplicate
        """
        with _dedup_lock:
            dedup_map = self._get_map()
            dedup_map[symbol] = bar_open_time_ms
            self._save_map(dedup_map)

        logger.info(
            f"[Dedup] ✅ تم تسجيل إشارة — {symbol} "
            f"| bar_time={bar_open_time_ms}"
        )

    def check_and_lock(self, symbol: str, bar_open_time_ms: int) -> bool:
        """
        يتحقق من التكرار ويُسجّل الإشارة في خطوة واحدة atomic تحت نفس القفل.
        يمنع race condition في حال استُدعيت run_bot_iteration مرتين بالتوازي.

        يُفضَّل استخدامه بدلاً من استدعاء is_duplicate() ثم register_signal() منفصلين.

        Returns:
            True  → إشارة جديدة، تم تسجيلها (يمكنك المتابعة وفتح الصفقة)
            False → إشارة مكررة، تجاهلها (لا تفتح صفقة)
        """
        with _dedup_lock:
            dedup_map = self._get_map()
            last_bar  = dedup_map.get(symbol)

            if last_bar == bar_open_time_ms:
                logger.debug(
                    f"[Dedup] ⛔ إشارة مكررة — {symbol} "
                    f"| bar_time={bar_open_time_ms} | تجاهل"
                )
                return False

            # سجّل الإشارة فوراً داخل نفس القفل (atomic)
            dedup_map[symbol] = bar_open_time_ms
            self._save_map(dedup_map)

        logger.info(
            f"[Dedup] ✅ تم تسجيل إشارة (atomic) — {symbol} | bar_time={bar_open_time_ms}"
        )
        return True

    def clear_symbol(self, symbol: str):
        """
        امسح سجل الرمز بعد إغلاق صفقته.

        ادعُها من handle_trade_close لتسمح بإشارة جديدة
        عند الشمعة القادمة دون انتظار.

        Parameters:
            symbol: رمز العملة المراد مسحه
        """
        with _dedup_lock:
            dedup_map = self._get_map()
            if symbol in dedup_map:
                del dedup_map[symbol]
                self._save_map(dedup_map)
                logger.info(
                    f"[Dedup] 🗑️ تم مسح سجل {symbol} "
                    f"— جاهز لإشارة جديدة"
                )
            else:
                logger.debug(
                    f"[Dedup] clear_symbol({symbol}) — الرمز لم يكن مسجلاً"
                )

    def clear_all(self):
        """
        امسح كل السجلات (مفيد عند إعادة تهيئة البوت يدوياً).
        استخدم بحذر.
        """
        with _dedup_lock:
            self._save_map({})
        logger.warning("[Dedup] ⚠️ تم مسح كل سجلات الـ Dedup")

    def get_registered_symbols(self) -> dict:
        """
        يُرجع نسخة من كل الإشارات المسجلة حالياً.
        مفيد للـ debugging أو عرض الحالة في تيليجرام.

        Returns:
            dict: {"BTC/USDT": 1750298400000, ...}
        """
        with _dedup_lock:
            return dict(self._get_map())

    def is_symbol_locked(self, symbol: str) -> bool:
        """
        هل الرمز مقفل حالياً (تم تسجيل إشارة عليه ولم تُغلق الصفقة بعد)؟
        مفيد للـ dashboard أو الـ monitoring.
        """
        with _dedup_lock:
            return symbol in self._get_map()

    def get_stats(self) -> dict:
        """إحصائيات للـ debugging والمراقبة"""
        with _dedup_lock:
            dedup_map = self._get_map()
        return {
            "locked_symbols_count": len(dedup_map),
            "locked_symbols":       list(dedup_map.keys()),
        }

if __name__ == "__main__":
    pass
