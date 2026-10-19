"""
WsCandleEngine — محرك الشموع الحية عبر Websocket
يجلب الشموع التاريخية مرة واحدة عند التشغيل،
ثم يستمع للإغلاقات الحية لتحديث النافذة محلياً في الذاكرة.
المرحلة الثانية: بديل REST API البطيء.
"""
import asyncio
import json
import threading
import time
import pandas as pd
from collections import deque
from datetime import datetime
import websockets
from core.logger import logger


class WsCandleEngine:
    """
    نافذة شموع حية لزوج واحد.
    
    الاستخدام:
        engine = WsCandleEngine("BTCUSDT", "15m", history_df)
        engine.start()
        df = engine.get_candles()   # دائماً يُرجع آخر DataFrame محدث
    """

    WS_FUTURES_MAINNET = "wss://fstream.binance.com/ws"
    # Current official Binance USD-M Demo endpoint. Legacy
    # binancefuture.com hosts caused disconnect/fallback bursts.
    WS_FUTURES_TESTNET = "wss://demo-fstream.binance.com/ws"

    def __init__(self, symbol: str, interval: str, seed_df, max_candles: int = 260,
                 use_testnet: bool = False):
        """
        symbol     : اسم الزوج بدون / مثل "BTCUSDT"
        interval   : الفريم مثل "15m"
        seed_df    : DataFrame تاريخي مجلوب مسبقاً عبر REST (seed أولي)
        max_candles: حجم النافذة المحلية
        """
        self.symbol      = symbol.replace("/", "").upper()
        self.interval    = interval
        self.max_candles = max_candles
        self._lock       = threading.Lock()
        self._running    = False
        self._thread     = None
        self._ws_base    = self.WS_FUTURES_TESTNET if use_testnet else self.WS_FUTURES_MAINNET
        self._last_message_time = 0.0
        self._last_closed_time = 0.0

        # تهيئة النافذة من البيانات التاريخية
        self._candles: deque = deque(maxlen=max_candles)
        self.replace_seed(seed_df)
        logger.info(f"[WsCandle] {self.symbol} {interval}: تم تهيئة النافذة بـ {len(self._candles)} شمعة تاريخية.")

    def start(self):
        """يشغّل الاستماع في خيط daemon منفصل"""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"WsCandle-{self.symbol}"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def replace_seed(self, seed_df):
        """استبدال النافذة بلقطة REST Futures مرتبة ومنزوعة التكرار."""
        if seed_df is None or seed_df.empty:
            return False
        rows = []
        for _, row in seed_df.iterrows():
            item = row.to_dict()
            ts = item.get("timestamp", item.get("time"))
            if ts is None:
                continue
            item["timestamp"] = pd.to_datetime(ts)
            rows.append(item)
        rows.sort(key=lambda item: item["timestamp"])
        dedup = {item["timestamp"]: item for item in rows}
        with self._lock:
            self._candles.clear()
            for item in list(dedup.values())[-self.max_candles:]:
                self._candles.append(item)
        return True

    def is_fresh(self, max_age_seconds: float = 30.0) -> bool:
        return self._last_message_time > 0 and (
            time.time() - self._last_message_time
        ) <= max_age_seconds

    def get_candles(self):
        """يُرجع DataFrame من النافذة المحلية (thread-safe)"""
        with self._lock:
            if not self._candles:
                return pd.DataFrame()
            return pd.DataFrame(list(self._candles))

    def _upsert_candle(self, candle: dict):
        """تحديث الشمعة بواسطة open time؛ حدث الإغلاق يستبدل ولا يكرر."""
        timestamp = candle["timestamp"]
        if not self._candles:
            self._candles.append(candle)
            return

        last_timestamp = pd.to_datetime(self._candles[-1].get("timestamp"))
        if last_timestamp == timestamp:
            self._candles[-1] = candle
            return
        if timestamp > last_timestamp:
            self._candles.append(candle)
            return

        # رسالة متأخرة: حدّث نفس الشمعة إن كانت لا تزال داخل النافذة.
        for index in range(len(self._candles) - 1, -1, -1):
            if pd.to_datetime(self._candles[index].get("timestamp")) == timestamp:
                self._candles[index] = candle
                return

    # ── الاستماع الداخلي ────────────────────────────────────────

    def _run_loop(self):
        """حلقة إعادة الاتصال التلقائي عند انقطاع WS"""
        while self._running:
            try:
                asyncio.run(self._listen())
            except Exception as e:
                logger.warning(f"[WsCandle] {self.symbol}: انقطع الاتصال ({e}). إعادة المحاولة بعد 5s...")
                time.sleep(5)

    async def _listen(self):
        stream = f"{self.symbol.lower()}@kline_{self.interval}"
        url    = f"{self._ws_base}/{stream}"
        logger.info(f"[WsCandle] الاتصال بـ {url}")

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg  = json.loads(raw)
                    k    = msg.get("k", {})
                    is_closed = k.get("x", False)  # True = شمعة مغلقة

                    candle = {
                        "timestamp": pd.to_datetime(int(k["t"]), unit='ms'),  # توحيد مع fetch_ohlcv
                        "open":   float(k["o"]),
                        "high":   float(k["h"]),
                        "low":    float(k["l"]),
                        "close":  float(k["c"]),
                        "volume": float(k["v"]),
                    }

                    with self._lock:
                        self._upsert_candle(candle)
                    self._last_message_time = time.time()
                    if is_closed:
                        self._last_closed_time = self._last_message_time
                        logger.debug(
                            f"[WsCandle] {self.symbol}: شمعة Futures مغلقة حُدّثت | "
                            f"open={candle['open']} high={candle['high']} "
                            f"low={candle['low']} close={candle['close']} "
                            f"volume={candle['volume']}"
                        )
                        try:
                            from utils.decision_audit import audit_decision_event
                            audit_decision_event(
                                symbol=(
                                    f"{self.symbol[:-4]}/USDT"
                                    if self.symbol.endswith("USDT")
                                    else self.symbol
                                ),
                                stage="MARKET_BAR",
                                status="CLOSED",
                                details={
                                    "source": "BINANCE_USDM_FUTURES_WS",
                                    "timeframe": self.interval,
                                    "open_time": candle["timestamp"].isoformat(),
                                    "open": candle["open"],
                                    "high": candle["high"],
                                    "low": candle["low"],
                                    "close": candle["close"],
                                    "volume": candle["volume"],
                                },
                            )
                        except Exception as audit_exc:
                            logger.debug(
                                f"[WsCandle] تعذر تدقيق OHLC المغلق لـ "
                                f"{self.symbol}: {audit_exc}"
                            )

                except Exception as e:
                    logger.warning(f"[WsCandle] خطأ في معالجة رسالة WS: {e}")


class SharedCandleStream:
    """اتصال Futures واحد يضاعف kline streams لكل رموز جولة المسح."""

    # EMA200 is read from the last closed candle (-2).  A fresh WebSocket
    # message alone is therefore not enough to make a trading decision.
    MIN_DECISION_HISTORY = 210

    WS_FUTURES_MAINNET = "wss://fstream.binance.com/ws"
    WS_FUTURES_TESTNET = "wss://demo-fstream.binance.com/ws"

    def __init__(self, interval="15m", max_candles=260, use_testnet=False):
        self.interval = str(interval)
        self.max_candles = int(max_candles)
        self._ws_base = (
            self.WS_FUTURES_TESTNET if use_testnet else self.WS_FUTURES_MAINNET
        )
        self._lock = threading.RLock()
        self._symbols = set()
        self._candles = {}
        self._last_message_time = {}
        self._data_sources = {}
        self._running = False
        self._thread = None
        self._loop = None
        self._ws = None
        self._request_id = 0
        self._reconnect_count = 0

    @staticmethod
    def _clean_symbol(symbol):
        return str(symbol or "").replace("/", "").replace(":USDT", "").upper()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="SharedCandleStream",
        )
        self._thread.start()

    def stop(self, timeout=5.0):
        self._running = False
        loop = self._loop
        ws = self._ws
        if loop and loop.is_running() and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def set_symbols(self, symbols):
        wanted = {self._clean_symbol(symbol) for symbol in symbols if symbol}
        wanted.discard("")
        with self._lock:
            previous = set(self._symbols)
            added = wanted - previous
            removed = previous - wanted
            self._symbols = wanted
            for symbol in removed:
                self._candles.pop(symbol, None)
                self._last_message_time.pop(symbol, None)
                self._data_sources.pop(symbol, None)
        if (added or removed) and self._loop and self._loop.is_running() and self._ws:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._apply_remote_subscription(added, removed), self._loop
                )
                future.add_done_callback(self._consume_future_exception)
            except Exception as exc:
                logger.debug(f"[SharedCandle] تأجل تحديث الاشتراك حتى reconnect: {exc}")
        return {"added": added, "removed": removed, "total": len(wanted)}

    @staticmethod
    def _consume_future_exception(future):
        try:
            future.result()
        except Exception:
            pass

    def replace_seed(self, symbol, seed_df):
        symbol = self._clean_symbol(symbol)
        if not symbol or seed_df is None or seed_df.empty:
            return False
        dedup = {}
        for _, row in seed_df.iterrows():
            item = row.to_dict()
            timestamp = item.get("timestamp", item.get("time"))
            if timestamp is None:
                continue
            item["timestamp"] = pd.to_datetime(timestamp)
            dedup[item["timestamp"]] = item
        ordered = [dedup[key] for key in sorted(dedup)][-self.max_candles:]
        with self._lock:
            self._candles[symbol] = deque(ordered, maxlen=self.max_candles)
            self._data_sources[symbol] = "REST_SEED"
        return True

    def get_candles(self, symbol):
        symbol = self._clean_symbol(symbol)
        with self._lock:
            candles = list(self._candles.get(symbol, ()))
        return pd.DataFrame(candles)

    def needs_history_backfill(self, symbol, min_candles=None):
        required = int(min_candles or self.MIN_DECISION_HISTORY)
        symbol = self._clean_symbol(symbol)
        with self._lock:
            buffered = len(self._candles.get(symbol, ()))
        return buffered < required

    def is_fresh(self, symbol, max_age_seconds=30.0):
        symbol = self._clean_symbol(symbol)
        with self._lock:
            last = float(self._last_message_time.get(symbol, 0.0) or 0.0)
        return last > 0 and time.time() - last <= max_age_seconds

    def get_symbol_health(self, symbol, max_age_seconds=30.0):
        symbol = self._clean_symbol(symbol)
        with self._lock:
            last = float(self._last_message_time.get(symbol, 0.0) or 0.0)
            source = self._data_sources.get(symbol, "UNAVAILABLE")
            buffered = len(self._candles.get(symbol, ()))
        age = max(0.0, time.time() - last) if last else None
        return {
            "symbol": symbol,
            "source": source,
            "last_message_age_seconds": age,
            "status": "FRESH" if age is not None and age <= max_age_seconds else (
                "STALE" if buffered else "UNAVAILABLE"
            ),
            "buffered_candles": buffered,
        }

    def get_stats(self):
        with self._lock:
            now = time.time()
            ages = [max(0.0, now - value) for value in self._last_message_time.values() if value]
            return {
                "symbols": len(self._symbols),
                "buffers": len(self._candles),
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "connected": self._ws is not None,
                "reconnect_count": self._reconnect_count,
                "fresh_symbols_30s": sum(age <= 30 for age in ages),
                "stale_symbols_30s": sum(age > 30 for age in ages),
                "max_message_age_seconds": max(ages) if ages else None,
            }

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._listen_forever())
        finally:
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

    async def _listen_forever(self):
        while self._running:
            try:
                await self._listen_once()
            except Exception as exc:
                if not self._running:
                    break
                self._reconnect_count += 1
                logger.warning(
                    f"[SharedCandle] انقطاع #{self._reconnect_count}: {exc}; "
                    "إعادة الاتصال بعد 5s"
                )
                try:
                    from utils.runtime_health import log_memory_milestone
                    log_memory_milestone(
                        "WEBSOCKET_DISCONNECT",
                        force=True,
                        details={"stream": "shared_candles", "reconnect": self._reconnect_count},
                    )
                except Exception:
                    pass
                await asyncio.sleep(5)

    async def _listen_once(self):
        logger.info(
            f"[SharedCandle] اتصال واحد لـ {len(self._symbols)} رمز | {self._ws_base}"
        )
        try:
            from utils.runtime_health import log_memory_milestone
            log_memory_milestone("WEBSOCKET_RECONNECT_START", force=True, details={"stream": "shared_candles"})
        except Exception:
            pass
        async with websockets.connect(
            self._ws_base, ping_interval=20, ping_timeout=10, close_timeout=5
        ) as ws:
            self._ws = ws
            with self._lock:
                current = set(self._symbols)
            if current:
                await self._send_subscription("SUBSCRIBE", current)
            logger.info(f"[SharedCandle] ✅ متصل؛ subscriptions={len(current)}")
            try:
                from utils.runtime_health import log_memory_milestone
                log_memory_milestone(
                    "WEBSOCKET_RECONNECT_END", force=True,
                    details={"stream": "shared_candles", "symbols": len(current)},
                )
            except Exception:
                pass
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    self._process_message(raw)
            finally:
                self._ws = None

    async def _apply_remote_subscription(self, added, removed):
        if removed:
            await self._send_subscription("UNSUBSCRIBE", removed)
        if added:
            await self._send_subscription("SUBSCRIBE", added)

    async def _send_subscription(self, method, symbols):
        ws = self._ws
        if ws is None or not symbols:
            return
        self._request_id += 1
        await ws.send(json.dumps({
            "method": method,
            "params": [
                f"{symbol.lower()}@kline_{self.interval}" for symbol in sorted(symbols)
            ],
            "id": self._request_id,
        }))

    def _process_message(self, raw):
        try:
            message = json.loads(raw)
            if message.get("e") != "kline":
                return
            symbol = self._clean_symbol(message.get("s"))
            kline = message.get("k") or {}
            candle = {
                "timestamp": pd.to_datetime(int(kline["t"]), unit="ms"),
                "open": float(kline["o"]),
                "high": float(kline["h"]),
                "low": float(kline["l"]),
                "close": float(kline["c"]),
                "volume": float(kline["v"]),
            }
            with self._lock:
                if symbol not in self._symbols:
                    return
                candles = self._candles.setdefault(
                    symbol, deque(maxlen=self.max_candles)
                )
                if candles and pd.to_datetime(candles[-1].get("timestamp")) == candle["timestamp"]:
                    candles[-1] = candle
                elif not candles or candle["timestamp"] > pd.to_datetime(candles[-1].get("timestamp")):
                    candles.append(candle)
                self._last_message_time[symbol] = time.time()
                self._data_sources[symbol] = "BINANCE_USDM_FUTURES_SHARED_WS"

            if kline.get("x"):
                logger.debug(
                    f"[SharedCandle] {symbol}: closed {self.interval} "
                    f"O={candle['open']} H={candle['high']} L={candle['low']} "
                    f"C={candle['close']} V={candle['volume']}"
                )
                try:
                    from utils.decision_audit import audit_decision_event
                    audit_decision_event(
                        symbol=(
                            f"{symbol[:-4]}/USDT" if symbol.endswith("USDT") else symbol
                        ),
                        stage="MARKET_BAR",
                        status="CLOSED",
                        details={
                            "source": "BINANCE_USDM_FUTURES_SHARED_WS",
                            "timeframe": self.interval,
                            "open_time": candle["timestamp"].isoformat(),
                            **{key: candle[key] for key in ("open", "high", "low", "close", "volume")},
                        },
                    )
                except Exception as exc:
                    logger.debug(f"[SharedCandle] تعذر تدقيق شمعة {symbol}: {exc}")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            logger.debug(f"[SharedCandle] رسالة غير صالحة: {exc}")
