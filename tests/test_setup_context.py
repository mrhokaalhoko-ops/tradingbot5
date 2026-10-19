import numpy as np
import pandas as pd

from strategy.setup_context import analyze_setup_context_1h, select_setup_side


def _breakout_bars(close_value=102.5):
    closed = 40
    close = np.full(closed + 1, 100.0)
    open_price = np.full(closed + 1, 99.9)
    high = np.full(closed + 1, 101.0)
    low = np.full(closed + 1, 99.0)
    volume = np.ones(closed + 1)
    open_price[-2] = 100.4
    high[-2] = 103.0
    low[-2] = 100.2
    close[-2] = close_value
    volume[-2] = 3.0
    # Forming candle must not alter the classification.
    open_price[-1] = close[-1] = high[-1] = low[-1] = 80.0
    volume[-1] = 999.0
    return pd.DataFrame({
        "time": pd.date_range("2026-08-01", periods=closed + 1, freq="1h", tz="UTC"),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


def test_closed_hourly_breakout_is_distinct_from_forming_candle():
    context = analyze_setup_context_1h(_breakout_bars())
    selected = select_setup_side(context, "buy")

    assert context["known"] is True
    assert context["long_state"] == "CONFIRMED_BREAKOUT"
    assert context["rvol_20"] == 3.0
    assert selected["selected_state"] == "CONFIRMED_BREAKOUT"
    assert selected["closed_candle_only"] is True


def test_wick_above_resistance_is_not_a_confirmed_breakout():
    context = analyze_setup_context_1h(_breakout_bars(close_value=100.5))

    assert context["long_state"] == "WICK_BREAK"
    assert context["long_state"] != "CONFIRMED_BREAKOUT"


def test_hourly_setup_missing_history_is_unknown_not_false_confirmation():
    context = analyze_setup_context_1h(_breakout_bars().head(10))

    assert context["known"] is False
    assert context["long_state"] == "UNKNOWN"
