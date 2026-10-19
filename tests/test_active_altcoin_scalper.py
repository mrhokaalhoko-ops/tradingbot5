from datetime import datetime, timedelta, timezone

import pandas as pd

from trading_v2.active_altcoin_scalper import (
    ActiveAltcoinConfig,
    replay_event,
    select_hot_events,
    summarize_records,
)


UTC = timezone.utc


def _frame(symbol="ALTUSDT", *, falling=False):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(20):
        open_price = 100.0
        close = 100.0
        if index == 5:
            close = 103.0 if not falling else 97.0
        if index > 5:
            open_price = close = 103.0 if not falling else 97.0
        high = max(open_price, close) + 0.1
        low = min(open_price, close) - 0.1
        rows.append({
            "open_time": start + timedelta(minutes=5 * index),
            "close_time": start + timedelta(minutes=5 * (index + 1)) - timedelta(milliseconds=1),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "symbol": symbol,
            "observation_valid": True,
            "return_1_pct": 3.0 if index == 5 and not falling else (-3.0 if index == 5 else 0.0),
            "return_4_pct": 4.0 if index == 5 and not falling else (-4.0 if index == 5 else 0.0),
            "volume_ratio": 3.0 if index == 5 else 1.0,
            "quote_volume": 1_000_000.0,
            "body_pct": 0.94 if index == 5 else 0.0,
            "close_location": 0.98 if not falling else 0.02,
            "atr": 1.0,
            "global_regime": "RANGE",
        })
    return pd.DataFrame(rows)


def test_selects_hot_altcoin_but_not_major():
    alt = _frame()
    major = _frame("BTCUSDT")
    selected = select_hot_events(pd.concat([alt, major], ignore_index=True))
    assert list(selected["symbol"]) == ["ALTUSDT"]
    assert list(selected["side"]) == ["LONG"]
    assert selected.iloc[0]["body_fraction"] > 0.9


def test_wide_wick_event_is_rejected_even_when_body_move_exceeds_half_percent():
    frame = _frame()
    signal = frame.index[frame["return_1_pct"].ne(0)][0]
    frame.loc[signal, ["open", "high", "low", "close", "body_pct"]] = [
        100.0, 106.0, 96.0, 103.0, 3.0
    ]

    selected = select_hot_events(frame)

    assert selected.empty


def test_immediate_continuation_replay_reaches_target():
    frame = _frame()
    selected = select_hot_events(frame)
    event = selected.iloc[0]
    # The next bar reaches the frozen 1.5R target without touching the stop.
    signal_location = frame.index[frame["close_time"].eq(event["close_time"])][0]
    frame.loc[signal_location + 1, "high"] = 105.0
    result = replay_event(event, frame, "IMMEDIATE_CONTINUATION")
    assert result["status"] == "TP_FIRST"
    assert result["net_r"] < 1.5
    assert result["net_r"] > 1.0


def test_same_bar_ambiguity_is_not_counted_as_win():
    frame = _frame()
    event = select_hot_events(frame).iloc[0]
    signal_location = frame.index[frame["close_time"].eq(event["close_time"])][0]
    frame.loc[signal_location + 1, ["low", "high"]] = [101.0, 105.0]
    result = replay_event(event, frame, "IMMEDIATE_CONTINUATION")
    assert result["status"] == "AMBIGUOUS_AS_LOSS"
    assert result["net_r"] < -1.0


def test_summary_reports_profit_factor_and_drawdown():
    rows = [
        {"net_r": 1.0, "side": "LONG", "status": "TP_FIRST"},
        {"net_r": -0.5, "side": "SHORT", "status": "SL_FIRST"},
    ]
    summary = summarize_records(rows)
    assert summary["positions"] == 2
    assert summary["profit_factor"] == 2.0
    assert summary["max_drawdown_r"] == 0.5
    assert summary["statuses"] == {"TP_FIRST": 1, "SL_FIRST": 1}
