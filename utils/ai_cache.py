import threading
import time
from cachetools import TTLCache
from core.logger import logger

# ── إعدادات الكاش (قابلة للتعديل) ───────────────────────────────
_AI_TTL_SECONDS  = 300    # 5 دقائق — تحليل AI يظل صالحاً هذه المدة
_AI_MAX_SYMBOLS  = 100    # أقصى عدد رموز في الكاش في نفس الوقت


class AIAnalysisCache:
    """
    كاش Thread-Safe لنتائج تحليل الذكاء الاصطناعي.

    يُنشأ كـ Singleton في ai_engine.py ويُستخدم عبر كل الخيوط.
    """

    def __init__(self, ttl_seconds: int = _AI_TTL_SECONDS, max_symbols: int = _AI_MAX_SYMBOLS):
        self._cache = TTLCache(maxsize=max_symbols, ttl=ttl_seconds)
        self._lock  = threading.Lock()
        self._ttl   = ttl_seconds

        # إحصائيات للمراقبة
        self._hits   = 0
        self._misses = 0

        logger.debug(
            f"[AICache] تم تهيئة الكاش | "
            f"TTL={ttl_seconds}s | Max={max_symbols} رمز"
        )

    # ─────────────────────────────────────────────────────────────
    # API الرئيسية
    # ─────────────────────────────────────────────────────────────

    def get(self, symbol: str) -> dict | None:
        """
        يُرجع التحليل المخزن أو None إذا انتهت صلاحيته / غير موجود.
        """
        with self._lock:
            result = self._cache.get(symbol)

        if result is not None:
            self._hits += 1
            logger.debug(
                f"[AICache] ✅ HIT — {symbol} "
                f"| توفير API call "
                f"| إجمالي hits: {self._hits}"
            )
        else:
            self._misses += 1
            logger.debug(
                f"[AICache] ❌ MISS — {symbol} "
                f"| سيتم استدعاء Google Genai"
            )

        return result

    def set(self, symbol: str, analysis: dict):
        """
        يحفظ نتيجة التحليل في الكاش مع إضافة timestamp تلقائياً.
        """
        analysis_with_ts = {
            **analysis,
            "_cached_at": int(time.time()),
            "_symbol":    symbol,
        }

        with self._lock:
            self._cache[symbol] = analysis_with_ts

        logger.debug(
            f"[AICache] 💾 تم حفظ تحليل {symbol} "
            f"| ينتهي خلال {self._ttl}s"
        )

    def invalidate(self, symbol: str):
        """
        امسح تحليل رمز معين يدوياً.
        """
        with self._lock:
            removed = self._cache.pop(symbol, None)

        if removed:
            logger.info(f"[AICache] 🗑️ تم مسح تحليل {symbol} يدوياً")
        else:
            logger.debug(f"[AICache] invalidate({symbol}) — الرمز لم يكن في الكاش")

    def invalidate_all(self):
        """
        امسح كل الكاش.
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
        logger.warning(f"[AICache] ⚠️ تم مسح كل الكاش ({count} رمز)")

    # ─────────────────────────────────────────────────────────────
    # المراقبة والإحصائيات
    # ─────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        with self._lock:
            cached_symbols = list(self._cache.keys())
            cache_size     = len(self._cache)

        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0.0

        return {
            "cache_size":     cache_size,
            "max_size":       self._cache.maxsize,
            "ttl_seconds":    self._ttl,
            "cached_symbols": cached_symbols,
            "hits":           self._hits,
            "misses":         self._misses,
            "hit_rate_pct":   round(hit_rate, 1),
        }

    def is_cached(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._cache

    def get_remaining_ttl(self, symbol: str) -> float:
        with self._lock:
            if symbol not in self._cache:
                return 0.0
            cached_at = self._cache[symbol].get("_cached_at", 0)

        elapsed   = time.time() - cached_at
        remaining = max(0.0, self._ttl - elapsed)
        return round(remaining, 1)

if __name__ == "__main__":
    pass
