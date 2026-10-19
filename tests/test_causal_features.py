from datetime import datetime, timedelta, timezone

import pandas as pd

from trading_v2.causal_features import (
    add_forward_labels,
    add_structure_features,
    add_timeframe_features,
    aggregate_timeframe,
)


def _frame(rows: int = 60) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=5 * i) for i in range(rows)]
    close = [100.0 + i * 0.1 for i in range(rows)]
    return pd.DataFrame(
        {
            "open_time": times,
            "open": close,
            "high": [value + 0.2 for value in close],
            "low": [value - 0.2 for value in close],
            "close": close,
            "volume": [10.0] * rows,
            "quote_volume": [1000.0] * rows,
        }
    )


def test_aggregate_timeframe_has_closed_bar_count_and_no_future_data():
    result = aggregate_timeframe(_frame(6), 15)

    assert len(result) == 2
    assert result.iloc[0]["source_bars"] == 3
    assert result.iloc[0]["close"] == 100.2
    assert result.iloc[0]["close_time"] > result.iloc[0]["open_time"]


def test_structure_sweep_uses_prior_window_only():
    frame = _frame()
    frame.loc[30, ["high", "close"]] = 110.0
    result = add_structure_features(frame)

    assert result.loc[29, "breakout_up_24"] == 0
    assert result.loc[30, "breakout_up_24"] == 1


def test_indicator_and_forward_labels_do_not_use_future_close():
    frame = _frame(100)
    result = add_forward_labels(add_timeframe_features(frame))
    baseline = result.loc[40, "ema20"]
    frame.loc[99, "close"] = 10_000.0
    changed = add_forward_labels(add_timeframe_features(frame))

    assert baseline == changed.loc[40, "ema20"]
    assert pd.isna(result.iloc[-1]["future_close_1x5m_pct"])
    assert result.loc[40, "future_entry_open_1x5m"] == frame.loc[41, "open"]
    assert result.loc[40, "future_close_1x5m_from_entry_pct"] == 0.0
