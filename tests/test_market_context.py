import numpy as np
import pandas as pd

from strategy.market_context import analyze_market_context_4h


def _bars(closed_points=240):
    closes = np.linspace(100.0, 150.0, closed_points)
    frame = pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=closed_points + 1, freq="4h", tz="UTC"),
        "open": np.r_[closes - 0.4, 150.0],
        "high": np.r_[closes + 0.8, 151.0],
        "low": np.r_[closes - 0.8, 1.0],
        "close": np.r_[closes, 1.0],
        "volume": np.ones(closed_points + 1),
    })
    return frame


def test_4h_context_uses_last_closed_candle_not_forming_crash():
    context = analyze_market_context_4h(_bars())

    assert context["known"] is True
    assert context["trend"] == "BULLISH"
    assert context["close"] == 150.0
    assert context["closed_candle_only"] is True
    assert context["last_closed_at"].startswith("2026-")


def test_4h_context_marks_large_distance_from_mean_as_extended():
    context = analyze_market_context_4h(_bars())

    assert context["distance_from_ema_atr"] > 2.5
    assert context["extended"] is True


def test_4h_context_fails_closed_on_short_history():
    context = analyze_market_context_4h(_bars(closed_points=50))

    assert context["known"] is False
    assert context["trend"] == "UNKNOWN"
    assert context["reason"] == "INSUFFICIENT_4H_HISTORY"
