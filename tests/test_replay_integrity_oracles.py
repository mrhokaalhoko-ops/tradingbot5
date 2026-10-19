"""Independent arithmetic and time-boundary oracles for the research simulator."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from trading_v2.evaluation import EvaluatedPlan, summarize
from trading_v2.models import Bar, EntryKind, ReplayResult, ReplayStatus, Side, TradePlan
from trading_v2.replay import CausalReplayEngine, ReplayCosts

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ZERO = ReplayCosts(0, 0, 0, 0, 0)


def bar(minute, o=100, h=101, l=99, c=100):
    return Bar("AUDIT/USDT", "5m", T0 + timedelta(minutes=minute),
               T0 + timedelta(minutes=minute + 5), o, h, l, c, 10)


def plan(side=Side.BUY, *, kind=EntryKind.MARKET_NEXT_OPEN, expiry=10, end=15):
    return TradePlan("audit", "audit", "audit", "AUDIT/USDT", side,
                     T0, T0 + timedelta(minutes=expiry), T0 + timedelta(minutes=end),
                     kind, 100, 98 if side is Side.BUY else 102,
                     104 if side is Side.BUY else 96, "hand-calculated oracle")


def test_prices_after_holding_deadline_cannot_change_outcome():
    p = plan(end=10)
    prefix = [bar(5, c=100.5)]
    before = CausalReplayEngine(ZERO).replay_plan(p, prefix)
    after = CausalReplayEngine(ZERO).replay_plan(p, prefix + [bar(10, 100.5, 110, 99, 109)])
    assert before == after
    assert after.status is ReplayStatus.OPEN_AT_HORIZON
    assert after.exited_at == T0 + timedelta(minutes=10)
    assert after.net_r == .25


def test_explicit_horizon_overrides_later_plan_exit():
    result = CausalReplayEngine(ZERO).replay_plan(
        plan(end=30), [bar(5), bar(10, 100, 110, 99, 109)],
        horizon_end=T0 + timedelta(minutes=10),
    )
    assert result.status is ReplayStatus.OPEN_AT_HORIZON
    assert result.exited_at == T0 + timedelta(minutes=10)
    assert result.net_r == 0


def test_partial_horizon_is_unknown_and_never_uses_future_extremes():
    p = plan(end=12)
    a = CausalReplayEngine(ZERO).replay_plan(p, [bar(5), bar(10)])
    b = CausalReplayEngine(ZERO).replay_plan(p, [bar(5), bar(10, 100, 110, 99, 109)])
    assert a == b
    assert a.status.value == "INCOMPLETE_DATA"
    assert a.net_r is None


def test_dataset_ending_before_exit_does_not_invent_a_time_exit():
    result = CausalReplayEngine(ZERO).replay_plan(plan(end=60), [bar(5)])
    assert result.status.value == "INCOMPLETE_DATA"
    assert result.filled_at == T0 + timedelta(minutes=5)
    assert result.exited_at is None
    assert result.net_r is None


def test_limit_touch_after_entry_expiry_cannot_create_a_fill():
    result = CausalReplayEngine(ZERO).replay_plan(
        plan(kind=EntryKind.LIMIT, expiry=10, end=20),
        [bar(5, 102, 103, 101, 102), bar(10, 102, 103, 99, 101)],
    )
    assert result.status is ReplayStatus.EXPIRED_UNFILLED
    assert result.filled_at is None


@pytest.mark.parametrize("side,opened,high,low,closed,expected", [
    (Side.BUY, 95, 97, 94, 96, -2.5),
    (Side.SELL, 105, 106, 103, 104, -2.5),
])
def test_gap_through_stop_uses_worse_open_not_unavailable_stop(side, opened, high, low, closed, expected):
    result = CausalReplayEngine(ZERO).replay_plan(
        plan(side), [bar(5), bar(10, opened, high, low, closed)],
    )
    assert result.status is ReplayStatus.SL_FIRST
    assert result.exit_price == opened
    assert result.net_r == expected
    assert result.exited_at == T0 + timedelta(minutes=10)


def test_stop_at_open_resolves_before_later_target_touch():
    result = CausalReplayEngine(ZERO).replay_plan(
        plan(), [bar(5), bar(10, 95, 110, 94, 109)],
    )
    assert result.status is ReplayStatus.SL_FIRST
    assert result.net_r == -2.5


def test_known_stop_at_open_can_resolve_before_partial_horizon():
    result = CausalReplayEngine(ZERO).replay_plan(
        plan(end=12), [bar(5), bar(10, 95, 110, 94, 109)],
    )
    assert result.status is ReplayStatus.SL_FIRST
    assert result.net_r == -2.5


def test_missing_bar_after_entry_cannot_hide_an_unseen_stop():
    result = CausalReplayEngine(ZERO).replay_plan(
        plan(end=25), [bar(5), bar(15, 100, 105, 99, 104)],
    )
    assert result.status.value == "INCOMPLETE_DATA"
    assert result.net_r is None


def test_duplicate_bar_cannot_create_a_second_price_path():
    result = CausalReplayEngine(ZERO).replay_plan(
        plan(end=15), [bar(5), bar(5, 100, 105, 99, 104), bar(10)],
    )
    assert result.status.value == "INCOMPLETE_DATA"
    assert result.net_r is None


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_round_trip_fees_match_hand_calculation(side):
    # Entry 100, risk 2, gross target gain 4. Fees are .05 + exit*.0005.
    target_bar = bar(10, 100, 105, 99, 104) if side is Side.BUY else bar(10, 100, 101, 95, 96)
    result = CausalReplayEngine(ReplayCosts(5, 5, 0, 0, 0)).replay_plan(plan(side), [bar(5), target_bar])
    assert result.gross_r == 2
    assert result.net_r == (1.949 if side is Side.BUY else 1.951)


def test_identical_closed_prices_lose_the_exact_round_trip_cost():
    result = CausalReplayEngine(ReplayCosts(5, 5, 0, 0, 0)).replay_plan(plan(end=10), [bar(5)])
    assert result.net_r == -.05


def record(name, created_minute, exit_minute, pnl):
    p = replace(plan(end=60), plan_id=name, setup_id=name,
                created_at=T0 + timedelta(minutes=created_minute))
    result = ReplayResult(name, ReplayStatus.TP_FIRST if pnl > 0 else ReplayStatus.SL_FIRST,
                          exited_at=T0 + timedelta(minutes=exit_minute), net_r=pnl)
    return EvaluatedPlan(p, result)


def test_drawdown_uses_realization_order_not_plan_creation():
    # Creation order: -1,+2,-1 => wrong DD 1. Close order: +2,-1,-1 => DD 2.
    rows = [record("early-plan-late-loss", 0, 30, -1),
            record("later-plan-first-win", 1, 10, 2), record("last-loss", 2, 40, -1)]
    assert summarize(rows)["max_drawdown_r"] == 2
    assert summarize(reversed(rows))["max_drawdown_r"] == 2


def test_simultaneous_closes_are_one_equity_observation():
    rows = [record("a", 0, 30, -1), record("b", 1, 30, 1)]
    assert summarize(rows)["max_drawdown_r"] == 0


def test_missing_exit_time_makes_drawdown_unavailable():
    row = record("a", 0, 30, -1)
    row = replace(row, result=replace(row.result, exited_at=None))
    metrics = summarize([row])
    assert metrics["total_net_r"] == -1
    assert metrics["max_drawdown_r"] is None


@pytest.mark.parametrize("field", ["entry_fee_bps", "exit_fee_bps", "entry_slippage_bps", "stop_slippage_bps", "target_slippage_bps", "market_exit_slippage_bps"])
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_costs_are_rejected(field, value):
    with pytest.raises(ValueError):
        ReplayCosts(**{field: value})


def test_binance_inclusive_millisecond_closes_are_contiguous():
    bars = [replace(b, close_time=b.close_time - timedelta(milliseconds=1)) for b in [bar(5), bar(10)]]
    result = CausalReplayEngine(ZERO).replay_plan(plan(end=15), bars)
    assert result.status is ReplayStatus.OPEN_AT_HORIZON
    assert result.net_r == 0
    assert result.exited_at == T0 + timedelta(minutes=15) - timedelta(milliseconds=1)


def test_wrong_execution_bar_duration_is_not_assumed_complete():
    wrong = replace(bar(5), close_time=T0 + timedelta(minutes=9))
    result = CausalReplayEngine(ZERO).replay_plan(plan(), [wrong, bar(10)])
    assert result.status is ReplayStatus.INCOMPLETE_DATA


def test_nonfinite_realized_return_cannot_pass_performance_metrics():
    row = record("invalid", 0, 10, float("nan"))
    with pytest.raises(ValueError, match="finite"):
        summarize([row])


def test_unknown_exit_retains_portfolio_slot_after_entry_expiry():
    from trading_v2.evaluation import enforce_portfolio_capacity
    first = record("open", 0, 30, -1)
    first = replace(first, result=ReplayResult(
        "open", ReplayStatus.INCOMPLETE_DATA, filled_at=T0 + timedelta(minutes=5)))
    second = record("later", 1, 30, 1)
    second = replace(second, plan=replace(second.plan, symbol="OTHER/USDT"),
                     result=replace(second.result, filled_at=T0 + timedelta(minutes=15)))
    selected = enforce_portfolio_capacity([first, second], max_open_positions=1)
    assert selected.rejected_capacity == 1
    assert [r.plan.plan_id for r in selected.records] == ["open"]
    assert summarize(selected.records)["realized"] == 0


def test_strategy_runner_does_not_reenter_when_filled_exit_is_unknown():
    from unittest.mock import Mock
    from trading_v2.data import CausalBarStore
    from trading_v2.research import evaluate_strategy
    strategy, replayer = Mock(), Mock()
    strategy.evaluate.side_effect = lambda snapshot: [replace(
        plan(expiry=10, end=40), created_at=snapshot.as_of,
        valid_until=snapshot.as_of + timedelta(minutes=1),
        plan_id=str(snapshot.as_of),
    )]
    replayer.replay_plan.return_value = ReplayResult(
        "unknown", ReplayStatus.INCOMPLETE_DATA, filled_at=T0 + timedelta(minutes=6))
    store = CausalBarStore("AUDIT/USDT", {"5m":[bar(i) for i in range(0, 25, 5)]})
    rows = evaluate_strategy(strategy, store, start=T0, end=T0 + timedelta(minutes=25),
                             regime_at=lambda at: "RANGE", execution_timeframe="5m", replay=replayer)
    assert len(rows) == 1
    strategy.evaluate.assert_called_once()
