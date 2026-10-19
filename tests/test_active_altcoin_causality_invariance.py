"""Prefix-invariance checks inspired by Freqtrade lookahead analysis."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from strategy.active_altcoin_shadow import detect_active_event


def _event_frame() -> pd.DataFrame:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for index in range(130):
        open_price = price
        close = price
        quote = 20_000.0
        if 120 <= index <= 124:
            close = 100.0 + (index - 119) * 1.0
            quote = 100_000.0
        high = max(open_price, close) + 0.03
        low = min(open_price, close) - 0.03
        price = close
        rows.append(
            {
                "open_time": start + timedelta(minutes=index),
                "close_time": start
                + timedelta(minutes=index + 1)
                - timedelta(milliseconds=1),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": quote / close,
                "quote_volume": quote,
                "taker_buy_quote_volume": quote * 0.51,
            }
        )
    return pd.DataFrame(rows)


def test_event_is_invariant_to_rows_after_decision_time():
    frame = _event_frame()
    now = frame.iloc[124]["close_time"]
    baseline = detect_active_event("ALTUSDT", frame, now)

    # Deliberately impossible future candles.  A causal event at `now` must be
    # byte-for-byte identical despite them.
    changed = frame.copy()
    changed.loc[125:, ["open", "high", "low", "close"]] = [
        1_000_000.0,
        2_000_000.0,
        0.0001,
        1_500_000.0,
    ]
    changed.loc[125:, ["quote_volume", "taker_buy_quote_volume"]] = [
        10_000_000_000.0,
        10_000_000_000.0,
    ]

    assert detect_active_event("ALTUSDT", changed, now) == baseline
