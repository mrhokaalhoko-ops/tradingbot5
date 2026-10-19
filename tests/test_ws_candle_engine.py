import time
import unittest

import pandas as pd

from unittest.mock import Mock, patch

from engines.ws_candle_engine import SharedCandleStream, WsCandleEngine
from utils.websocket_manager import _WS_URL_TESTNET


class TestWsCandleEngine(unittest.TestCase):
    def _seed(self):
        return pd.DataFrame([
            {
                "timestamp": pd.Timestamp("2026-01-01 00:00:00"),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01 00:15:00"),
                "open": 100.5,
                "high": 102.0,
                "low": 100.0,
                "close": 101.0,
                "volume": 12.0,
            },
        ])

    def test_uses_futures_endpoints(self):
        live = WsCandleEngine("BTCUSDT", "15m", self._seed(), use_testnet=False)
        demo = WsCandleEngine("BTCUSDT", "15m", self._seed(), use_testnet=True)

        self.assertIn("fstream.binance.com", live._ws_base)
        self.assertEqual(demo._ws_base, "wss://demo-fstream.binance.com/ws")
        self.assertEqual(
            _WS_URL_TESTNET,
            "wss://demo-fstream.binance.com/ws/!markPrice@arr@1s",
        )
        self.assertNotIn("stream.binance.com:9443", live._ws_base)

    def test_closed_update_replaces_same_timestamp(self):
        engine = WsCandleEngine("BTCUSDT", "15m", self._seed())
        closed = {
            "timestamp": pd.Timestamp("2026-01-01 00:15:00"),
            "open": 100.5,
            "high": 103.0,
            "low": 100.0,
            "close": 102.5,
            "volume": 20.0,
        }

        with engine._lock:
            engine._upsert_candle(closed)

        candles = engine.get_candles()
        self.assertEqual(len(candles), 2)
        self.assertEqual(float(candles.iloc[-1]["close"]), 102.5)

    def test_new_timestamp_appends_once(self):
        engine = WsCandleEngine("BTCUSDT", "15m", self._seed())
        live = {
            "timestamp": pd.Timestamp("2026-01-01 00:30:00"),
            "open": 102.5,
            "high": 103.0,
            "low": 102.0,
            "close": 102.8,
            "volume": 5.0,
        }
        with engine._lock:
            engine._upsert_candle(live)
            engine._upsert_candle({**live, "close": 102.9})

        candles = engine.get_candles()
        self.assertEqual(len(candles), 3)
        self.assertEqual(float(candles.iloc[-1]["close"]), 102.9)

    def test_freshness_is_explicit(self):
        engine = WsCandleEngine("BTCUSDT", "15m", self._seed())
        self.assertFalse(engine.is_fresh())
        engine._last_message_time = time.time()
        self.assertTrue(engine.is_fresh())

    def test_shared_stream_bounds_buffers_to_current_universe(self):
        stream = SharedCandleStream()
        stream.set_symbols(["BTC/USDT", "ETH/USDT"])
        stream.replace_seed("BTC/USDT", self._seed())
        stream.replace_seed("ETH/USDT", self._seed())

        change = stream.set_symbols(["ETH/USDT", "SOL/USDT"])

        self.assertEqual(change["total"], 2)
        self.assertTrue(stream.get_candles("BTC/USDT").empty)
        self.assertEqual(len(stream.get_candles("ETH/USDT")), 2)

    def test_shared_stream_updates_one_symbol_from_raw_kline(self):
        stream = SharedCandleStream()
        stream.set_symbols(["BTC/USDT"])
        stream.replace_seed("BTC/USDT", self._seed())
        stream._process_message(
            '{"e":"kline","s":"BTCUSDT","k":{"t":1767227400000,'
            '"o":"101","h":"104","l":"100","c":"103","v":"25","x":false}}'
        )

        candles = stream.get_candles("BTC/USDT")
        self.assertEqual(float(candles.iloc[-1]["close"]), 103.0)
        self.assertTrue(stream.is_fresh("BTC/USDT"))

    def test_fresh_single_websocket_candle_still_requires_history_backfill(self):
        stream = SharedCandleStream()
        stream.set_symbols(["BTC/USDT"])
        stream.replace_seed("BTC/USDT", self._seed().iloc[:1])
        stream._last_message_time["BTCUSDT"] = time.time()

        self.assertTrue(stream.is_fresh("BTC/USDT"))
        self.assertTrue(stream.needs_history_backfill("BTC/USDT"))

        history = pd.concat([self._seed()] * 110, ignore_index=True)
        history["timestamp"] = pd.date_range(
            "2026-01-01", periods=len(history), freq="15min"
        )
        stream.replace_seed("BTC/USDT", history)
        self.assertFalse(stream.needs_history_backfill("BTC/USDT"))

    def test_shared_stream_start_is_idempotent_one_thread(self):
        stream = SharedCandleStream()
        fake_thread = Mock()
        with patch("engines.ws_candle_engine.threading.Thread", return_value=fake_thread) as cls:
            stream.start()
            stream.start()

        cls.assert_called_once()
        fake_thread.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
