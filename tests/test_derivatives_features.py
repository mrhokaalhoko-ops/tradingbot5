import pandas as pd
import pytest

from trading_v2.derivatives_features import (
    add_metrics_features,
    attach_derivatives_features,
)


def test_metrics_change_requires_complete_five_minute_history():
    times = pd.date_range("2026-01-01T00:00:00Z", periods=13, freq="5min")
    frame = pd.DataFrame({
        "metrics_time": times,
        "symbol": ["BTCUSDT"] * 13,
        "sum_open_interest": [100.0] * 12 + [110.0],
        "sum_open_interest_value": [1000.0] * 12 + [1100.0],
        "count_toptrader_long_short_ratio": [1.0] * 13,
        "sum_toptrader_long_short_ratio": [1.0] * 13,
        "count_long_short_ratio": [1.0] * 13,
        "sum_taker_long_short_vol_ratio": [1.0] * 13,
    })
    result = add_metrics_features(frame)
    assert pd.isna(result.iloc[11]["oi_value_change_1h_pct"])
    assert result.iloc[12]["oi_value_change_1h_pct"] == pytest.approx(10.0)


def test_asof_join_never_uses_future_metrics_snapshot():
    observations = pd.DataFrame({
        "close_time": pd.to_datetime([
            "2026-01-01T00:04:59.999Z", "2026-01-01T00:09:59.999Z",
        ]),
        "close": [100.0, 101.0],
        "symbol": ["BTCUSDT", "BTCUSDT"],
    })
    metrics = pd.DataFrame({
        "metrics_time": pd.to_datetime([
            "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z",
        ]),
        "sum_open_interest_value": [1000.0, 2000.0],
        "symbol": ["BTCUSDT", "BTCUSDT"],
    })
    funding = pd.DataFrame({
        "funding_time": pd.to_datetime(["2026-01-01T00:00:00Z"]),
        "last_funding_rate": [0.0001],
    })
    result = attach_derivatives_features(observations, metrics, funding)
    assert result["sum_open_interest_value"].tolist() == [1000.0, 2000.0]
    assert result["symbol"].tolist() == ["BTCUSDT", "BTCUSDT"]
    assert (result["metrics_time"] <= result["close_time"]).all()
    assert (result["funding_time"] <= result["close_time"]).all()


def test_asof_join_handles_completely_missing_derivatives_sources():
    observations = pd.DataFrame({
        "close_time": pd.to_datetime(["2026-01-01T00:04:59.999Z"]),
        "close": [100.0],
    })
    result = attach_derivatives_features(
        observations,
        add_metrics_features(pd.DataFrame()),
        pd.DataFrame(),
    )
    assert pd.isna(result.iloc[0]["sum_open_interest_value"])
    assert pd.isna(result.iloc[0]["last_funding_rate"])
