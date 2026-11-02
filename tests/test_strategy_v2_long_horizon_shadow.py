import pandas as pd
import pytest

from strategy_v2.long_horizon_shadow import advance_symbol_state, new_symbol_state
from strategy_v2.long_horizon_trend import LongHorizonTrendPolicy, simulate_symbol


def _bars(count=9):
    times = pd.date_range("2026-01-01", periods=count, freq="4h", tz="UTC")
    close = [10, 10, 10, 10, 12, 12.5, 13, 13.5, 12][:count]
    return pd.DataFrame(
        {
            "open_time": times,
            "close_time": times + pd.Timedelta(hours=4),
            "open": [10, 10, 10, 10, 12.1, 12.4, 12.8, 13.4, 12.2][:count],
            "high": [11, 11, 11, 11, 12.5, 13, 13.5, 14, 12.5][:count],
            "low": [9, 9, 9, 9, 11.5, 12, 12.5, 13, 11.5][:count],
            "close": close,
            "volume": [100] * count,
        }
    )


def _policy():
    return LongHorizonTrendPolicy(
        entry_lookback_bars=4,
        exit_lookback_bars=2,
        atr_bars=2,
        initial_stop_atr=1,
        trailing_stop_atr=2,
        funding_drag_bps_per_8h=0,
    )


def test_first_observation_only_establishes_warmup():
    policy = _policy()
    state, events = advance_symbol_state(
        new_symbol_state("BTCUSDT", policy),
        _bars(5),
        observed_at="2026-01-01T20:00:30Z",
        policy=policy,
    )
    assert [event["event_type"] for event in events] == ["WARMUP_ESTABLISHED"]
    assert state["pending_entry"] is None
    assert events[0]["execution_authority"] is False


def test_signal_is_persistable_before_next_bar_entry():
    policy = _policy()
    state = new_symbol_state("BTCUSDT", policy)
    state["last_processed_open_time"] = "2026-01-01T12:00:00+00:00"
    state, events = advance_symbol_state(
        state,
        _bars(5),
        observed_at="2026-01-01T20:00:30Z",
        policy=policy,
    )
    assert [event["event_type"] for event in events] == ["SHADOW_SIGNAL"]
    assert state["pending_entry"]["side"] == "LONG"

    state, events = advance_symbol_state(
        state,
        _bars(6),
        observed_at="2026-01-02T00:00:30Z",
        policy=policy,
    )
    assert events[0]["event_type"] == "SHADOW_ENTRY"
    assert state["position"]["entry_time"] == "2026-01-01T20:00:00+00:00"
    assert all(event["execution_authority"] is False for event in events)


def test_late_observation_never_creates_retrospective_signal():
    policy = _policy()
    state = new_symbol_state("ETHUSDT", policy)
    state["last_processed_open_time"] = "2026-01-01T12:00:00+00:00"
    state, events = advance_symbol_state(
        state,
        _bars(5),
        observed_at="2026-01-01T21:00:00Z",
        policy=policy,
    )
    assert [event["event_type"] for event in events] == ["LATE_OBSERVATION"]
    assert state["pending_entry"] is None


def test_restart_replay_does_not_duplicate_processed_bar():
    policy = _policy()
    state = new_symbol_state("BTCUSDT", policy)
    state["last_processed_open_time"] = "2026-01-01T12:00:00+00:00"
    state, first = advance_symbol_state(
        state, _bars(5), observed_at="2026-01-01T20:00:20Z", policy=policy
    )
    restarted, second = advance_symbol_state(
        state, _bars(5), observed_at="2026-01-01T20:00:40Z", policy=policy
    )
    assert first
    assert second == ()
    assert restarted == state


def test_gap_and_insufficient_recovery_history_fail_closed():
    policy = _policy()
    frame = _bars(6)
    state = new_symbol_state("BTCUSDT", policy)
    state["last_processed_open_time"] = frame.iloc[0].open_time.isoformat()
    broken = frame.drop(index=2).reset_index(drop=True)
    with pytest.raises(ValueError, match="contiguous"):
        advance_symbol_state(
            state, broken, observed_at=frame.iloc[-1].close_time, policy=policy
        )

    state["last_processed_open_time"] = (
        pd.Timestamp(frame.iloc[0].open_time) - pd.Timedelta(hours=4)
    ).isoformat()
    with pytest.raises(ValueError, match="predates"):
        advance_symbol_state(
            state, frame, observed_at=frame.iloc[-1].close_time, policy=policy
        )


def test_clock_before_close_is_recorded_and_cannot_signal():
    policy = _policy()
    frame = _bars(5)
    state = new_symbol_state("BTCUSDT", policy)
    state["last_processed_open_time"] = frame.iloc[-2].open_time.isoformat()
    updated, events = advance_symbol_state(
        state,
        frame,
        observed_at=pd.Timestamp(frame.iloc[-1].close_time) - pd.Timedelta(seconds=1),
        policy=policy,
    )
    assert updated["pending_entry"] is None
    assert [event["event_type"] for event in events] == ["CLOCK_ANOMALY"]


def test_hourly_long_only_profile_accepts_long_and_suppresses_other_sides():
    policy = _policy()
    frame = _bars(5)
    frame["open_time"] = pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC")
    frame["close_time"] = frame["open_time"] + pd.Timedelta(hours=1)
    state = new_symbol_state("BTCUSDT", policy)
    state["last_processed_open_time"] = frame.iloc[-2].open_time.isoformat()
    updated, events = advance_symbol_state(
        state,
        frame,
        observed_at=frame.iloc[-1].close_time + pd.Timedelta(seconds=30),
        policy=policy,
        bar_interval="1h",
        allowed_sides=("LONG",),
    )
    assert [event["event_type"] for event in events] == ["SHADOW_SIGNAL"]
    assert updated["pending_entry"]["side"] == "LONG"

    suppressed, events = advance_symbol_state(
        state,
        frame,
        observed_at=frame.iloc[-1].close_time + pd.Timedelta(seconds=30),
        policy=policy,
        bar_interval="1h",
        allowed_sides=("SHORT",),
    )
    assert events == ()
    assert suppressed["pending_entry"] is None


def test_hourly_profile_rejects_non_contiguous_bars():
    policy = _policy()
    frame = _bars(5)
    frame["open_time"] = pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC")
    frame["close_time"] = frame["open_time"] + pd.Timedelta(hours=1)
    state = new_symbol_state("ETHUSDT", policy)
    state["last_processed_open_time"] = frame.iloc[0].open_time.isoformat()
    with pytest.raises(ValueError, match="contiguous"):
        advance_symbol_state(
            state,
            frame.drop(index=2).reset_index(drop=True),
            observed_at=frame.iloc[-1].close_time,
            policy=policy,
            bar_interval="1h",
        )


def test_incremental_shadow_matches_frozen_offline_trade_math():
    policy = _policy()
    times = pd.date_range("2026-01-01", periods=10, freq="4h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open_time": times,
            "close_time": times + pd.Timedelta(hours=4),
            "open": [10, 10, 10, 10, 12.1, 12.4, 12.8, 13.4, 12.2, 9.5],
            "high": [11, 11, 11, 11, 12.5, 13, 13.5, 14, 12.5, 10],
            "low": [9, 9, 9, 9, 11.5, 12, 12.5, 13, 11.5, 8.5],
            "close": [10, 10, 10, 10, 12, 12.5, 13, 13.5, 12, 9],
            "volume": [100] * 10,
        }
    )
    expected = simulate_symbol("BTCUSDT", frame, policy)
    assert len(expected) == 1

    state = new_symbol_state("BTCUSDT", policy)
    state["last_processed_open_time"] = frame.iloc[3].open_time.isoformat()
    completed = []
    for end in range(5, len(frame) + 1):
        bar = frame.iloc[end - 1]
        state, events = advance_symbol_state(
            state,
            frame.iloc[:end],
            observed_at=pd.Timestamp(bar.close_time) + pd.Timedelta(seconds=30),
            policy=policy,
        )
        completed.extend(
            event["trade"] for event in events if event["event_type"] == "SHADOW_EXIT"
        )
    assert len(completed) == 1
    actual = completed[0]
    reference = expected[0]
    assert actual["side"] == reference.side
    assert actual["entry_time"] == reference.entry_time
    assert actual["exit_time"] == reference.exit_time
    assert actual["exit_reason"] == reference.exit_reason
    assert actual["entry_price"] == pytest.approx(reference.entry_price)
    assert actual["exit_price"] == pytest.approx(reference.exit_price)
    assert actual["net_r"] == pytest.approx(reference.net_r)


def test_hourly_long_only_shadow_matches_offline_trade_math():
    policy = _policy()
    frame = _bars(9)
    frame["open_time"] = pd.date_range("2026-01-01", periods=len(frame), freq="1h", tz="UTC")
    frame["close_time"] = frame["open_time"] + pd.Timedelta(hours=1)
    expected = tuple(trade for trade in simulate_symbol("BTCUSDT", frame, policy) if trade.side == "LONG")
    assert len(expected) == 1

    state = new_symbol_state("BTCUSDT", policy)
    state["last_processed_open_time"] = frame.iloc[3].open_time.isoformat()
    completed = []
    for end in range(5, len(frame) + 1):
        bar = frame.iloc[end - 1]
        state, events = advance_symbol_state(
            state,
            frame.iloc[:end],
            observed_at=pd.Timestamp(bar.close_time) + pd.Timedelta(seconds=30),
            policy=policy,
            bar_interval="1h",
            allowed_sides=("LONG",),
        )
        completed.extend(
            event["trade"] for event in events if event["event_type"] == "SHADOW_EXIT"
        )
    assert len(completed) == 1
    assert completed[0]["entry_time"] == expected[0].entry_time
    assert completed[0]["exit_time"] == expected[0].exit_time
    assert completed[0]["net_r"] == pytest.approx(expected[0].net_r)
