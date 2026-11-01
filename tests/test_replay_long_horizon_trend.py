from strategy_v2.long_horizon_trend import LongHorizonTrendPolicy, TrendTrade
import pandas as pd

from scripts.replay_long_horizon_trend import (
    contiguous_hourly_segments,
    metrics,
    split_name,
)


def _trade(value, entry="2025-01-01T00:00:00+00:00"):
    return TrendTrade(
        symbol="BTCUSDT",
        side="LONG",
        signal_time=entry,
        entry_time=entry,
        exit_time="2025-01-02T00:00:00+00:00",
        entry_price=100,
        initial_stop=90,
        exit_price=100 + value * 10,
        exit_reason="TEST",
        risk=10,
        gross_r=value,
        fee_drag_r=0,
        funding_drag_r=0,
        net_r=value,
        holding_hours=24,
        policy_hash=LongHorizonTrendPolicy().policy_hash,
    )


def test_time_split_boundaries_are_fixed_before_holdout():
    assert split_name("2023-12-31T23:00:00+00:00") == "DEVELOPMENT"
    assert split_name("2024-06-01T00:00:00+00:00") == "VALIDATION"
    assert split_name("2025-01-01T00:00:00+00:00") == "HOLDOUT"


def test_metrics_use_net_r_and_drawdown():
    result = metrics([_trade(1), _trade(-0.5), _trade(-0.5), _trade(2)])
    assert result["trades"] == 4
    assert result["mean_net_r"] == 0.5
    assert result["profit_factor"] == 3.0
    assert result["max_drawdown_r"] == 1.0


def test_hourly_gap_starts_a_fresh_indicator_segment():
    frame = pd.DataFrame(
        {
            "open_time": pd.to_datetime(
                [
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T01:00:00Z",
                    "2026-01-01T05:00:00Z",
                ]
            ),
            "open": [1, 2, 3],
        }
    )
    segments = contiguous_hourly_segments(frame)
    assert [len(segment) for segment in segments] == [2, 1]
