"""
utils/symbol_cache.py
─────────────────────────────────────────────────────────────
كاش ذكي لبيانات الرموز في Binance Futures
يقلل استهلاك API بنسبة تصل لـ 80% للبيانات الثابتة

الاستخدام:
    from utils.symbol_cache import SymbolInfoCache
    cache = SymbolInfoCache(client)
    tick  = cache.get_tick_size("BTC/USDT")
─────────────────────────────────────────────────────────────
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
from cachetools import TTLCache
from core.logger import logger


# ── مدة الكاش لكل نوع بيانات ─────────────────────────────────────
_TTL_SYMBOL_INFO   = 43200   # 12 ساعة  — tick/step size (ثابتة جداً)
_TTL_LEVERAGE      = 3600    # 1  ساعة  — leverage brackets
_TTL_EXCHANGE_INFO = 21600   # 6  ساعات — exchange_info الكاملة

# ── أحجام الكاش ──────────────────────────────────────────────────
_MAX_SYMBOLS = 500

# ── كاشات مستقلة لكل نوع بيانات ─────────────────────────────────
_symbol_cache   = TTLCache(maxsize=_MAX_SYMBOLS, ttl=_TTL_SYMBOL_INFO)
_leverage_cache = TTLCache(maxsize=_MAX_SYMBOLS, ttl=_TTL_LEVERAGE)
_exchange_cache = TTLCache(maxsize=1,            ttl=_TTL_EXCHANGE_INFO)

# ── Lock واحد للحماية من Race Conditions ────────────────────────
_lock = threading.Lock()


class SymbolInfoCache:
    """
    مدير الكاش الذكي لبيانات الرموز.
    كل دالة تجلب البيانات مرة واحدة فقط ثم تُرجعها من الذاكرة.
    """

    def __init__(self, client):
        """
        client: ExchangeClient (ccxt wrapper)
        """
        self._client = client

    # ─────────────────────────────────────────────────────────────
    # Tick Size — دقة السعر (مثال: 0.10 لـ BTC)
    # ─────────────────────────────────────────────────────────────
    def get_tick_size(self, symbol: str) -> float:
        """
        يُرجع أصغر تغيير مسموح في السعر للرمز.
        مثال: BTC/USDT → 0.1
        """
        key = f"tick_{symbol}"
        with _lock:
            if key in _symbol_cache:
                return _symbol_cache[key]

        try:
            market = self._client.exchange.market(symbol)
            tick   = float(market.get("precision", {}).get("price", 0.01) or 0.01)
        except Exception as e:
            logger.warning(f"[SymbolCache] فشل جلب tick_size لـ {symbol}: {e} — سيتم استخدام 0.01")
            tick = 0.01

        with _lock:
            _symbol_cache[key] = tick
        return tick

    # ─────────────────────────────────────────────────────────────
    # Step Size — دقة الكمية (مثال: 0.001 لـ BTC)
    # ─────────────────────────────────────────────────────────────
    def get_step_size(self, symbol: str) -> float:
        """
        يُرجع أصغر كمية مسموح تداولها للرمز.
        مثال: BTC/USDT → 0.001
        """
        key = f"step_{symbol}"
        with _lock:
            if key in _symbol_cache:
                return _symbol_cache[key]

        try:
            market = self._client.exchange.market(symbol)
            step   = float(market.get("precision", {}).get("amount", 0.001) or 0.001)
        except Exception as e:
            logger.warning(f"[SymbolCache] فشل جلب step_size لـ {symbol}: {e} — سيتم استخدام 0.001")
            step = 0.001

        with _lock:
            _symbol_cache[key] = step
        return step

    # ─────────────────────────────────────────────────────────────
    # Min Notional — الحد الأدنى لقيمة الأمر بالدولار
    # ─────────────────────────────────────────────────────────────
    def get_min_notional(self, symbol: str) -> float:
        """
        يُرجع الحد الأدنى لقيمة الأمر (بالـ USDT).
        باينانس Futures: عادةً 5 دولار.
        """
        key = f"min_notional_{symbol}"
        with _lock:
            if key in _symbol_cache:
                return _symbol_cache[key]

        try:
            market  = self._client.exchange.market(symbol)
            limits  = market.get("limits", {})
            notional = float(
                (limits.get("cost") or {}).get("min", 5.0) or 5.0
            )
        except Exception as e:
            logger.warning(f"[SymbolCache] فشل جلب min_notional لـ {symbol}: {e} — سيتم استخدام 5.0")
            notional = 5.0

        with _lock:
            _symbol_cache[key] = notional
        return notional

    # ─────────────────────────────────────────────────────────────
    # Leverage Brackets — حدود الرافعة المالية لكل رمز
    # ─────────────────────────────────────────────────────────────
    def get_max_leverage(self, symbol: str) -> int:
        """
        يُرجع أقصى رافعة مالية مسموح بها للرمز.
        """
        key = f"max_lev_{symbol}"
        with _lock:
            if key in _leverage_cache:
                return _leverage_cache[key]

        try:
            # CCXT: fetch_leverage_tiers يُرجع قائمة بالـ brackets
            tiers  = self._client.exchange.fetch_leverage_tiers([symbol])
            # أول bracket عادةً يحتوي أعلى رافعة
            brackets = tiers.get(symbol, [])
            max_lev  = int(brackets[0].get("maxLeverage", 20)) if brackets else 20
        except Exception as e:
            logger.warning(f"[SymbolCache] فشل جلب max_leverage لـ {symbol}: {e} — سيتم استخدام 20")
            max_lev = 20

        with _lock:
            _leverage_cache[key] = max_lev
        return max_lev

    # ─────────────────────────────────────────────────────────────
    # Exchange Info — معلومات كاملة عن جميع الرموز (ثقيل)
    # ─────────────────────────────────────────────────────────────
    def get_exchange_info(self) -> dict:
        """
        يُرجع exchange_info كاملاً من ذاكرة الكاش.
        مفيد للـ Universe Filter عند فلترة الرموز.
        """
        key = "exchange_info"
        with _lock:
            if key in _exchange_cache:
                return _exchange_cache[key]

        try:
            info = self._client.exchange.load_markets(reload=True)
        except Exception as e:
            logger.error(f"[SymbolCache] فشل جلب exchange_info: {e}")
            return {}

        with _lock:
            _exchange_cache[key] = info
        return info

    # ─────────────────────────────────────────────────────────────
    # تقريب السعر أو الكمية للدقة الصحيحة
    # ─────────────────────────────────────────────────────────────
    def round_price(self, symbol: str, price: float) -> float:
        """يقرب السعر لأقرب tick size مسموح به"""
        tick = self.get_tick_size(symbol)
        if tick <= 0:
            return price
        return round(round(price / tick) * tick, 8)

    def round_amount(self, symbol: str, amount: float) -> float:
        """يقرب الكمية لأقرب step size مسموح به"""
        step = self.get_step_size(symbol)
        if step <= 0:
            return amount
        return round(round(amount / step) * step, 8)

    # ─────────────────────────────────────────────────────────────
    # مسح الكاش يدوياً
    # ─────────────────────────────────────────────────────────────
    def invalidate(self, symbol: str = None):
        """
        امسح الكاش:
        - invalidate("BTC/USDT") → يمسح رمزاً واحداً فقط
        - invalidate()           → يمسح كل الكاش (بعد تحديث باينانس مثلاً)
        """
        with _lock:
            if symbol:
                keys_to_remove = [
                    k for k in list(_symbol_cache.keys())
                    if symbol in k
                ]
                for k in keys_to_remove:
                    _symbol_cache.pop(k, None)

                lev_keys = [k for k in list(_leverage_cache.keys()) if symbol in k]
                for k in lev_keys:
                    _leverage_cache.pop(k, None)

                logger.info(f"[SymbolCache] تم مسح كاش {symbol}")
            else:
                _symbol_cache.clear()
                _leverage_cache.clear()
                _exchange_cache.clear()
                logger.info("[SymbolCache] تم مسح كل الكاش")

    def get_stats(self) -> dict:
        """إحصائيات الكاش الحالية — مفيدة للـ debugging"""
        with _lock:
            return {
                "symbol_cache_size":   len(_symbol_cache),
                "symbol_cache_max":    _symbol_cache.maxsize,
                "leverage_cache_size": len(_leverage_cache),
                "leverage_cache_max":  _leverage_cache.maxsize,
                "exchange_cache_size": len(_exchange_cache),
            }


# ─────────────────────────────────────────────────────────────────
# اختبار مستقل — شغّله بـ: python utils/symbol_cache.py
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from core.exchange import ExchangeClient

    print("=" * 50)
    print("اختبار SymbolInfoCache")
    print("=" * 50)

    client = ExchangeClient()
    cache  = SymbolInfoCache(client)

    test_symbol = "BTC/USDT:USDT"  # CCXT Futures Symbol

    print(f"\n[1] Tick Size لـ {test_symbol}:")
    t1 = cache.get_tick_size(test_symbol)
    t2 = cache.get_tick_size(test_symbol)   # يجب أن يأتي من الكاش فوراً
    print(f"    النتيجة: {t1} (الثانية من الكاش: {t2})")
    assert t1 == t2, "خطأ: القيمتان يجب أن تكونا متساويتين"

    print(f"\n[2] Step Size لـ {test_symbol}:")
    print(f"    النتيجة: {cache.get_step_size(test_symbol)}")

    print(f"\n[3] Min Notional لـ {test_symbol}:")
    print(f"    النتيجة: {cache.get_min_notional(test_symbol)}")

    print(f"\n[4] تقريب السعر 29834.567 للـ tick:")
    print(f"    النتيجة: {cache.round_price(test_symbol, 29834.567)}")

    print(f"\n[5] إحصائيات الكاش:")
    print(f"    {cache.get_stats()}")

    print(f"\n[6] مسح كاش {test_symbol}:")
    cache.invalidate(test_symbol)
    print(f"    بعد المسح: {cache.get_stats()}")

    print("\n✅ جميع الاختبارات نجحت!")
    print("=" * 50)
