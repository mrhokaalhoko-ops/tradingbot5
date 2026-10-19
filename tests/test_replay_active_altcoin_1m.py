from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from scripts.replay_active_altcoin_1m import replay_one
from trading_v2.active_altcoin_scalper import ActiveAltcoinConfig


def _bars():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(100):
        open_price = 100.0
        close = 100.0
        if index == 0:
            close = 103.0
        if index > 0:
            open_price = close = 103.0
        rows.append({
            "open_time": start + timedelta(minutes=index),
            "close_time": start + timedelta(minutes=index, seconds=59, milliseconds=999),
            "open": open_price,
            "high": max(open_price, close) + 0.1,
            "low": min(open_price, close) - 0.1,
            "close": close,
            "volume": 100.0,
            "quote_volume": 10_000.0,
            "trades": 50,
            "taker_buy_quote_volume": 6_000.0,
        })
    return pd.DataFrame(rows)


def _event():
    return pd.Series({
        "symbol": "ALTUSDT", "side": "LONG",
        "close_time": pd.Timestamp("2026-01-01T00:00:59.999Z"),
        "close": 103.0, "atr": 1.0, "event_score": 9.0,
        "return_1_pct": 3.0, "return_4_pct": 4.0,
        "volume_ratio": 3.0, "quote_volume": 1_000_000.0,
        "global_regime": "RANGE",
    })


def test_one_minute_immediate_replay_is_causal():
    bars = _bars()
    result = replay_one(_event(), bars, "IMMEDIATE_1M", ActiveAltcoinConfig())
    assert result["status"] == "TIME_EXIT"
    assert result["filled_at"] == bars.iloc[1]["open_time"]
    assert result["exited_at"] == bars.iloc[60]["close_time"]
    assert result["micro_directional_taker_imbalance_mean"] == pytest.approx(0.2)
    assert result["micro_directional_bar_fraction"] == 1.0
