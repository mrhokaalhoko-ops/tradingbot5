from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.replay_historical_entry_confirmation import _touches_zone, _trigger_window


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _frame(count=25):
    return pd.DataFrame([{
        "open_time": T0 + timedelta(minutes=5 * index),
        "close_time": T0 + timedelta(minutes=5 * (index + 1)) - timedelta(milliseconds=1),
        "timestamp": T0 + timedelta(minutes=5 * index),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 10.0, "global_regime": "RANGE",
        "target_month": "2026-01", "selection_rank": 1,
    } for index in range(count)])


def test_touch_requires_ohlc_overlap_with_the_zone():
    assert _touches_zone(pd.Series({"low": 99.8, "high": 100.2}), 100, 0.1)
    assert not _touches_zone(pd.Series({"low": 100.2, "high": 100.3}), 100, 0.1)


def test_trigger_window_places_candidate_closed_bar_at_minus_two():
    frame = _frame()
    result = _trigger_window(frame, 23)
    assert result.iloc[-2]["open_time"] == frame.iloc[23]["open_time"]
    assert result.iloc[-1]["open_time"] == frame.iloc[24]["open_time"]
    assert float(result.iloc[-1]["volume"]) == 0.0
