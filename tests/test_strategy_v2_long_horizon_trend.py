from dataclasses import replace

import pandas as pd

from strategy_v2.long_horizon_trend import (
    LongHorizonTrendPolicy,
    aggregate_hourly_to_4h,
    apply_portfolio_capacity,
    simulate_symbol,
)


def _hourly(count=8):
    times = pd.date_range("2026-01-01", periods=count, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": times,
            "open": range(1, count + 1),
            "high": [value + 1 for value in range(1, count + 1)],
            "low": [value - 1 for value in range(1, count + 1)],
            "close": [value + 0.5 for value in range(1, count + 1)],
            "volume": [10.0] * count,
        }
    )


def test_aggregate_hourly_requires_complete_four_bar_buckets():
    result = aggregate_hourly_to_4h(_hourly(7))
    assert len(result) == 1
    assert result.iloc[0].open == 1
    assert result.iloc[0].close == 4.5
    assert result.iloc[0].close_time == pd.Timestamp("2026-01-01T04:00:00Z")


def test_policy_hash_binds_cost_and_geometry():
    base = LongHorizonTrendPolicy(entry_lookback_bars=4, exit_lookback_bars=2, atr_bars=2)
    changed = replace(base, funding_drag_bps_per_8h=2.0)
    assert base.policy_hash != changed.policy_hash


def test_signal_enters_only_on_next_bar_and_has_no_execution_authority():
    policy = LongHorizonTrendPolicy(
        entry_lookback_bars=4,
        exit_lookback_bars=2,
        atr_bars=2,
        initial_stop_atr=1.0,
        trailing_stop_atr=2.0,
        funding_drag_bps_per_8h=0,
    )
    times = pd.date_range("2026-01-01", periods=10, freq="4h", tz="UTC")
    close = [10, 10, 10, 10, 12, 12.5, 13, 13.5, 12, 9]
    frame = pd.DataFrame(
        {
            "open_time": times,
            "close_time": times + pd.Timedelta(hours=4),
            "open": [10, 10, 10, 10, 12.1, 12.4, 12.8, 13.4, 12.2, 9.5],
            "high": [11, 11, 11, 11, 12.5, 13, 13.5, 14, 12.5, 10],
            "low": [9, 9, 9, 9, 11.5, 12, 12.5, 13, 11.5, 8.5],
            "close": close,
            "volume": [100] * 10,
        }
    )
    trades = simulate_symbol("TESTUSDT", frame, policy)
    assert len(trades) == 1
    trade = trades[0]
    assert pd.Timestamp(trade.signal_time) == pd.Timestamp("2026-01-01T20:00:00Z")
    # The completed signal bar ends exactly when the following bar opens.  The
    # fill therefore shares the boundary timestamp but uses the following
    # bar's open (12.4), never the signal bar's open (12.1).
    assert pd.Timestamp(trade.entry_time) == pd.Timestamp("2026-01-01T20:00:00Z")
    assert trade.entry_price > 12.4
    assert trade.execution_authority is False
    assert trade.policy_hash == policy.policy_hash


def test_capacity_is_deterministic_by_symbol():
    policy = LongHorizonTrendPolicy(entry_lookback_bars=4, exit_lookback_bars=2, atr_bars=2)
    from strategy_v2.long_horizon_trend import TrendTrade

    def trade(symbol):
        return TrendTrade(
            symbol=symbol,
            side="LONG",
            signal_time="2026-01-01T00:00:00+00:00",
            entry_time="2026-01-01T04:00:00+00:00",
            exit_time="2026-01-02T04:00:00+00:00",
            entry_price=100,
            initial_stop=90,
            exit_price=110,
            exit_reason="TEST",
            risk=10,
            gross_r=1,
            fee_drag_r=0,
            funding_drag_r=0,
            net_r=1,
            holding_hours=24,
            policy_hash=policy.policy_hash,
        )

    accepted = apply_portfolio_capacity((trade("B"), trade("A")), 1)
    assert [row.symbol for row in accepted] == ["A"]
