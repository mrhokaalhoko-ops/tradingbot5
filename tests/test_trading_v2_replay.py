from datetime import datetime, timedelta, timezone

import pytest

from trading_v2 import (
    Bar,
    CausalReplayEngine,
    EntryKind,
    ReplayCosts,
    ReplayStatus,
    Side,
    TradePlan,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(minutes, open_, high, low, close, timeframe="5m"):
    opened = T0 + timedelta(minutes=minutes)
    return Bar(
        symbol="TEST/USDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=opened + timedelta(minutes=5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
    )


def _plan(*, kind=EntryKind.LIMIT, created_minute=0, expiry_minute=30):
    return TradePlan(
        plan_id="plan-1",
        strategy_id="test-strategy",
        setup_id="setup-1",
        symbol="TEST/USDT",
        side=Side.BUY,
        created_at=T0 + timedelta(minutes=created_minute),
        valid_until=T0 + timedelta(minutes=expiry_minute),
        exit_by=T0 + timedelta(hours=4),
        entry_kind=kind,
        entry=100,
        stop=98,
        target=104,
        thesis="causal test",
    )


def test_snapshot_exposes_only_bars_closed_at_decision_time():
    bars = [
        _bar(0, 100, 101, 99, 100.5),
        _bar(5, 100.5, 102, 100, 101.5),
    ]
    snapshot = CausalReplayEngine.snapshot(
        symbol="TEST/USDT",
        as_of=T0 + timedelta(minutes=7),
        bars_by_timeframe={"5m": bars},
    )
    assert snapshot.closed_bars("5m") == (bars[0],)


def test_snapshot_rejects_future_data_if_constructed_directly():
    future = _bar(5, 100, 101, 99, 100)
    from trading_v2 import MarketSnapshot

    with pytest.raises(ValueError, match="future or still-open"):
        MarketSnapshot(
            symbol="TEST/USDT",
            as_of=T0 + timedelta(minutes=7),
            bars={"5m": (future,)},
        )


def test_limit_entry_and_target_same_bar_is_ambiguous_not_a_win():
    engine = CausalReplayEngine(ReplayCosts(0, 0, 0, 0, 0))
    result = engine.replay_plan(_plan(), [_bar(5, 101, 104.5, 99.5, 103)])
    assert result.status is ReplayStatus.AMBIGUOUS_ENTRY_BAR
    assert result.net_r is None


def test_target_before_limit_entry_cancels_the_plan():
    engine = CausalReplayEngine(ReplayCosts(0, 0, 0, 0, 0))
    result = engine.replay_plan(_plan(), [_bar(5, 101, 104.5, 100.5, 104)])
    assert result.status is ReplayStatus.TARGET_BEFORE_ENTRY


def test_market_plan_enters_next_open_then_reports_tp_first_net_of_costs():
    engine = CausalReplayEngine(ReplayCosts(5, 5, 0, 0, 0))
    result = engine.replay_plan(
        _plan(kind=EntryKind.MARKET_NEXT_OPEN),
        [
            _bar(5, 100, 103, 99, 102),
            _bar(10, 102, 104.2, 101, 104),
        ],
    )
    assert result.status is ReplayStatus.TP_FIRST
    assert result.entry_price == 100
    assert result.gross_r == 2.0
    assert result.net_r < result.gross_r


def test_same_bar_stop_and_target_is_not_guessed():
    engine = CausalReplayEngine(ReplayCosts(0, 0, 0, 0, 0))
    result = engine.replay_plan(
        _plan(kind=EntryKind.MARKET_NEXT_OPEN),
        [_bar(5, 100, 104.5, 97.5, 101)],
    )
    assert result.status is ReplayStatus.AMBIGUOUS_SAME_BAR
    assert result.net_r is None


def test_unfilled_limit_expires_without_inventing_a_trade():
    engine = CausalReplayEngine(ReplayCosts(0, 0, 0, 0, 0))
    result = engine.replay_plan(
        _plan(expiry_minute=15),
        [
            _bar(5, 102, 103, 101, 102),
            _bar(10, 102, 103, 101, 102),
            _bar(15, 102, 103, 101, 102),
        ],
    )
    assert result.status is ReplayStatus.EXPIRED_UNFILLED


def test_bar_that_started_before_decision_cannot_fill_the_plan():
    engine = CausalReplayEngine(ReplayCosts(0, 0, 0, 0, 0))
    plan = _plan(created_minute=7, expiry_minute=20)
    result = engine.replay_plan(
        plan,
        [
            _bar(5, 101, 102, 99.5, 100),  # touches entry before plan exists
            _bar(10, 102, 103, 101, 102),
            _bar(15, 102, 103, 101, 102),
        ],
    )
    assert result.status is ReplayStatus.EXPIRED_UNFILLED


def test_gap_through_stop_invalidates_limit_before_entry():
    engine = CausalReplayEngine(ReplayCosts(0, 0, 0, 0, 0))
    result = engine.replay_plan(
        _plan(),
        [_bar(5, 97, 100.5, 96, 100)],
    )
    assert result.status is ReplayStatus.INVALIDATED_BEFORE_ENTRY


def test_exit_by_bounds_holding_period():
    engine = CausalReplayEngine(ReplayCosts(0, 0, 0, 0, 0))
    plan = TradePlan(
        plan_id="bounded",
        strategy_id="test-strategy",
        setup_id="setup-bounded",
        symbol="TEST/USDT",
        side=Side.BUY,
        created_at=T0,
        valid_until=T0 + timedelta(minutes=10),
        exit_by=T0 + timedelta(minutes=12),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=100,
        stop=98,
        target=104,
        thesis="bounded horizon",
    )
    result = engine.replay_plan(
        plan,
        [
            _bar(5, 100, 101, 99, 100.5),
            _bar(10, 100.5, 101.5, 100, 101),
            _bar(15, 101, 104.5, 100.5, 104),
        ],
    )
    # 5m OHLC ending at minute 15 cannot price an exit at minute 12.
    assert result.status is ReplayStatus.INCOMPLETE_DATA
    assert result.bars_observed == 1
    assert result.exit_price is None
    assert result.net_r is None


def test_horizon_exit_charges_exit_fee_and_market_slippage():
    engine = CausalReplayEngine(
        ReplayCosts(
            entry_fee_bps=5,
            exit_fee_bps=5,
            entry_slippage_bps=0,
            stop_slippage_bps=0,
            target_slippage_bps=0,
            market_exit_slippage_bps=10,
        )
    )
    result = engine.replay_plan(
        _plan(kind=EntryKind.MARKET_NEXT_OPEN),
        [_bar(5, 100, 101, 99, 100)],
        horizon_end=T0 + timedelta(minutes=10),
    )

    assert result.status is ReplayStatus.OPEN_AT_HORIZON
    assert result.exit_price == 99.9
    assert result.net_r < result.gross_r < 0
