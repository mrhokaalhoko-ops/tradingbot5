"""
utils/websocket_manager.py
─────────────────────────────────────────────────────────────
مدير WebSocket للـ Binance Futures (USDS-M)
يستمع لـ Mark Prices لحظياً ويحفظها في الذاكرة

المزايا:
  - اتصال واحد فقط لكل الرموز (بدلاً من N طلب API)
  - Zero latency للأسعار اللحظية
  - إعادة اتصال تلقائية عند الانقطاع
  - آمن من Race Conditions بـ threading.Lock

الاستخدام في main.py:
    from utils.websocket_manager import FuturesWebSocketManager
    ws_manager = FuturesWebSocketManager()
    ws_manager.start()
    ...
    price = ws_manager.get_price("BTCUSDT")   # بدون / وبدون :USDT
─────────────────────────────────────────────────────────────
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import threading
import time
from datetime import datetime

import websockets
from core.logger import logger


# ── ثوابت الاتصال ─────────────────────────────────────────────────
_WS_URL_MARK_PRICE = "wss://fstream.binance.com/ws/!markPrice@arr@1s"
_WS_URL_TESTNET    = "wss://demo-fstream.binance.com/ws/!markPrice@arr@1s"
_RECONNECT_DELAY   = 3      # ثوانٍ قبل إعادة الاتصال
_MAX_RECONNECTS    = 50     # بعدها يُسجَّل تحذير حرج لكن يستمر
_PRICE_STALE_SEC   = max(
    2.0, float(os.getenv("MARK_PRICE_MAX_AGE_SECONDS", "10") or 10)
)  # stream cadence=1s؛ القيمة قابلة للضبط ويظهر العمر لكل caller


class FuturesWebSocketManager:
    """
    يستمع لـ !markPrice@arr@1s ويحفظ Mark Price لكل رمز.
    يعمل في خيط daemon منفصل — لا يعطل الحلقة الرئيسية.
    """

    def __init__(self, use_testnet: bool = False):
        self._url             = _WS_URL_TESTNET if use_testnet else _WS_URL_MARK_PRICE
        self._prices: dict    = {}     # symbol (BTCUSDT) → float
        self._timestamps: dict = {}    # symbol → float (وقت آخر تحديث)
        self._lock            = threading.Lock()
        self._running         = False
        self._reconnect_count = 0
        self._thread: threading.Thread = None
        self._loop: asyncio.AbstractEventLoop = None
        self._ws = None

        # إحصائيات للمراقبة
        self._messages_received = 0
        self._connected_since   = None
        self._fresh_hits = 0
        self._rest_fallbacks = 0
        self._stale_incidents = 0
        self._unavailable_incidents = 0
        self._fallback_failures = 0

    # ─────────────────────────────────────────────────────────────
    # API العامة — يستدعيها البوت
    # ─────────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        """
        يُرجع Mark Price للرمز من الذاكرة.
        symbol: بدون / وبدون :USDT — مثال: "BTCUSDT" أو "ETHUSDT"
        يُرجع 0.0 إذا لم يكن الرمز موجوداً بعد.
        """
        clean = self._clean_symbol(symbol)
        with self._lock:
            return self._prices.get(clean, 0.0)

    def get_price_snapshot(self, symbol: str) -> dict:
        """يرجع السعر وعمره وحالته كي لا تختلط البيانات القديمة بالحالية."""
        clean = self._clean_symbol(symbol)
        with self._lock:
            price = float(self._prices.get(clean, 0.0) or 0.0)
            last_time = float(self._timestamps.get(clean, 0.0) or 0.0)
        age = max(0.0, time.time() - last_time) if last_time > 0 else None
        if price <= 0 or age is None:
            status = "UNAVAILABLE"
        elif age <= _PRICE_STALE_SEC:
            status = "FRESH"
        else:
            status = "STALE"
        return {"price": price, "age_seconds": age, "status": status}

    def get_price_safe(self, symbol: str, fallback_fn=None, allow_stale=False) -> float:
        """
        يُرجع Mark Price، وإذا كان قديماً أو غير موجود يستدعي fallback_fn.
        fallback_fn: دالة تأخذ symbol وتُرجع سعراً (مثلاً client.get_current_price)
        """
        return float(self.get_price_with_source(
            symbol, fallback_fn=fallback_fn, allow_stale=allow_stale
        )["price"])

    def get_price_with_source(self, symbol: str, fallback_fn=None, allow_stale=False) -> dict:
        """Return price plus provenance/freshness for audit and fail-safe decisions."""
        snapshot = self.get_price_snapshot(symbol)
        price = snapshot["price"]
        if snapshot["status"] == "FRESH":
            with self._lock:
                self._fresh_hits += 1
            return {**snapshot, "source": "WS_FRESH", "fallback_error": None}

        with self._lock:
            if snapshot["status"] == "STALE":
                self._stale_incidents += 1
            else:
                self._unavailable_incidents += 1

        # السعر قديم أو غير موجود — استخدم الـ fallback
        fallback_error = None
        if fallback_fn is not None:
            try:
                fb_price = fallback_fn(symbol)
                if fb_price and fb_price > 0:
                    with self._lock:
                        self._rest_fallbacks += 1
                    logger.debug(f"[WS] استخدام fallback للسعر {symbol}: {fb_price}")
                    return {
                        "price": float(fb_price),
                        "age_seconds": snapshot.get("age_seconds"),
                        "status": "FRESH",
                        "source": "REST_FALLBACK",
                        "fallback_error": None,
                        "ws_status": snapshot.get("status"),
                    }
            except Exception as e:
                fallback_error = type(e).__name__
                with self._lock:
                    self._fallback_failures += 1
                logger.warning(f"[WS] فشل الـ fallback للسعر {symbol}: {e}")

        if allow_stale and price > 0:
            logger.warning(
                f"[WS] استخدام سعر قديم للعرض فقط {symbol}; "
                f"age={snapshot.get('age_seconds')}s"
            )
            return {
                **snapshot, "source": "STALE_CACHE", "fallback_error": fallback_error
            }
        # القرارات المالية لا تستخدم cache قديماً إذا فشل REST. صفر يعني
        # UNKNOWN ويدفع callers إلى الانتظار دون إبطال thesis أو تنفيذ/Trailing.
        return {
            "price": 0.0,
            "age_seconds": snapshot.get("age_seconds"),
            "status": "UNAVAILABLE",
            "source": "UNAVAILABLE",
            "fallback_error": fallback_error,
            "ws_status": snapshot.get("status"),
        }

    def is_connected(self) -> bool:
        """هل الاتصال نشط حالياً؟"""
        return self._running and self._connected_since is not None

    def is_price_fresh(self, symbol: str) -> bool:
        """هل السعر حديث (تحديث خلال آخر 10 ثوانٍ)؟"""
        clean = self._clean_symbol(symbol)
        with self._lock:
            last_time = self._timestamps.get(clean, 0.0)
        return (time.time() - last_time) <= _PRICE_STALE_SEC

    def get_all_prices(self) -> dict:
        """نسخة من كل الأسعار المحفوظة (للـ dashboard أو المراقبة)"""
        with self._lock:
            return dict(self._prices)

    def get_stats(self) -> dict:
        """إحصائيات للمراقبة والـ debugging"""
        with self._lock:
            symbols_count = len(self._prices)
        return {
            "connected":          self.is_connected(),
            "symbols_tracked":    symbols_count,
            "messages_received":  self._messages_received,
            "reconnect_count":    self._reconnect_count,
            "connected_since":    self._connected_since,
            "url":                self._url,
            "fresh_hits":         self._fresh_hits,
            "rest_fallbacks":     self._rest_fallbacks,
            "stale_incidents":    self._stale_incidents,
            "unavailable_incidents": self._unavailable_incidents,
            "fallback_failures":  self._fallback_failures,
        }

    # ─────────────────────────────────────────────────────────────
    # التشغيل والإيقاف
    # ─────────────────────────────────────────────────────────────

    def start(self):
        """
        يبدأ الاستماع في خيط daemon منفصل.
        آمن للاستدعاء أكثر من مرة — لن يشغّل خيطين.
        """
        if self._running:
            logger.warning("[WS] المدير يعمل مسبقاً، تم تجاهل start()")
            return

        self._running = True
        self._thread  = threading.Thread(
            target=self._run_in_thread,
            name="FuturesWSManager",
            daemon=True,   # يتوقف تلقائياً عند إغلاق البوت
        )
        self._thread.start()
        logger.info(f"[WS] ✅ تم تشغيل مدير WebSocket | URL: {self._url}")

        # انتظر حتى يبدأ الاستقبال (بحد أقصى 8 ثوانٍ)
        self._wait_for_first_data(timeout=8)

    def stop(self):
        """يوقف الاستماع بأمان"""
        self._running = False
        self._connected_since = None
        if self._loop and self._loop.is_running() and self._ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        logger.info("[WS] تم إيقاف مدير WebSocket")

    # ─────────────────────────────────────────────────────────────
    # المنطق الداخلي
    # ─────────────────────────────────────────────────────────────

    def _run_in_thread(self):
        """يشغّل حلقة asyncio داخل الخيط"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._listen_loop())
        except Exception as e:
            if self._running:
                logger.error(f"[WS] انهار خيط WebSocket: {e}")
            else:
                logger.debug(f"[WS] انتهت حلقة WebSocket أثناء الإيقاف: {e}")
        finally:
            # إغلاق نظيف لكل المهام المعلقة قبل إغلاق الحلقة
            try:
                pending = asyncio.all_tasks(loop=self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    async def _listen_loop(self):
        """الحلقة الرئيسية مع إعادة الاتصال التلقائية"""
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                if not self._running:
                    break
                self._reconnect_count += 1
                self._connected_since  = None
                try:
                    from utils.runtime_health import log_memory_milestone
                    log_memory_milestone(
                        "WEBSOCKET_DISCONNECT",
                        force=True,
                        details={"stream": "mark_price", "reconnect": self._reconnect_count},
                    )
                except Exception:
                    pass

                if self._reconnect_count >= _MAX_RECONNECTS:
                    logger.critical(
                        f"[WS] 🚨 {self._reconnect_count} انقطاع متتالٍ — "
                        f"تحقق من الاتصال بالإنترنت!"
                    )
                else:
                    logger.warning(
                        f"[WS] انقطاع #{self._reconnect_count}: {e} — "
                        f"إعادة المحاولة خلال {_RECONNECT_DELAY}s..."
                    )
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_listen(self):
        """
        يتصل ويستمع للـ stream.
        عند الانقطاع يُرفع exception ليمسك _listen_loop.
        """
        logger.info(f"[WS] جاري الاتصال بـ {self._url}...")
        try:
            from utils.runtime_health import log_memory_milestone
            log_memory_milestone("WEBSOCKET_RECONNECT_START", force=True, details={"stream": "mark_price"})
        except Exception:
            pass
        async with websockets.connect(
            self._url,
            ping_interval=20,    # باينانس يتوقع ping كل 20 ثانية
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected_since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._reconnect_count = 0   # إعادة ضبط العداد عند نجاح الاتصال
            logger.info(f"[WS] ✅ متصل بـ Binance Futures Mark Price Stream")
            try:
                from utils.runtime_health import log_memory_milestone
                log_memory_milestone("WEBSOCKET_RECONNECT_END", force=True, details={"stream": "mark_price"})
            except Exception:
                pass

            try:
                async for raw_message in ws:
                    if not self._running:
                        break
                    self._process_message(raw_message)
            finally:
                self._ws = None

    def _process_message(self, raw: str):
        """
        يُحلّل رسالة باينانس ويُحدّث الأسعار في الذاكرة.
        رسالة markPrice@arr تأتي كقائمة من الكائنات.
        """
        try:
            events = json.loads(raw)

            # باينانس يُرسل قائمة في !markPrice@arr
            if not isinstance(events, list):
                return

            now = time.time()
            with self._lock:
                for event in events:
                    if event.get("e") != "markPriceUpdate":
                        continue
                    symbol = event.get("s", "")   # مثال: BTCUSDT
                    price  = float(event.get("p", 0) or 0)
                    if symbol and price > 0:
                        self._prices[symbol]     = price
                        self._timestamps[symbol] = now

            self._messages_received += 1

        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug(f"[WS] رسالة غير صالحة: {e}")

    def _clean_symbol(self, symbol: str) -> str:
        """
        يُوحّد صيغة الرمز:
        "BTC/USDT"     → "BTCUSDT"
        "BTC/USDT:USDT"→ "BTCUSDT"
        "BTCUSDT"      → "BTCUSDT"
        """
        return symbol.replace("/", "").replace(":USDT", "").replace(":BTC", "").upper()

    def _wait_for_first_data(self, timeout: int = 8):
        """
        ينتظر حتى تصل أول بيانات من WebSocket.
        مفيد بعد start() للتأكد أن الاتصال ناجح قبل بدء البوت.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if len(self._prices) > 0:
                    logger.info(
                        f"[WS] ✅ بدأ استقبال الأسعار — "
                        f"{len(self._prices)} رمز في الذاكرة"
                    )
                    return
            time.sleep(0.5)

        logger.warning(
            f"[WS] ⚠️ لم تصل بيانات خلال {timeout} ثوانٍ — "
            f"البوت سيستخدم API كـ fallback"
        )


# ─────────────────────────────────────────────────────────────────
# اختبار مستقل — شغّله بـ: python utils/websocket_manager.py
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from core.config import Config

    print("=" * 55)
    print("اختبار FuturesWebSocketManager")
    print("=" * 55)

    use_testnet = getattr(Config, "USE_TESTNET", False)
    print(f"الوضع: {'TESTNET' if use_testnet else 'MAINNET'}")

    manager = FuturesWebSocketManager(use_testnet=use_testnet)
    manager.start()

    print("\n[1] انتظار 5 ثوانٍ لاستقبال بيانات...")
    time.sleep(5)

    stats = manager.get_stats()
    print(f"\n[2] إحصائيات الاتصال:")
    for k, v in stats.items():
        print(f"    {k}: {v}")

    test_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    print(f"\n[3] أسعار Mark Price:")
    for sym in test_symbols:
        price = manager.get_price(sym)
        fresh = manager.is_price_fresh(sym)
        print(f"    {sym}: {price:.4f} | حديث: {fresh}")

    print(f"\n[4] اختبار get_price_safe مع fallback:")
    price = manager.get_price_safe("BTCUSDT", fallback_fn=lambda s: 0.0)
    print(f"    BTCUSDT: {price:.4f}")

    print(f"\n[5] عدد الرموز في الذاكرة: {len(manager.get_all_prices())}")

    print(f"\n[6] اختبار إيقاف المدير...")
    manager.stop()
    time.sleep(1)
    print(f"    متصل بعد الإيقاف: {manager.is_connected()}")

    print("\n✅ الاختبار اكتمل!")
    print("=" * 55)
    print("\nطريقة الاستخدام في main.py:")
    print("  from utils.websocket_manager import FuturesWebSocketManager")
    print("  ws_manager = FuturesWebSocketManager(use_testnet=Config.USE_TESTNET)")
    print("  ws_manager.start()")
    print("  ...")
    print("  price = ws_manager.get_price_safe(symbol, fallback_fn=client.get_current_price)")
    print("=" * 55)
