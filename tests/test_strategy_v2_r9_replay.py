"""R9 causal, ambiguity-aware offline outcome replay tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_v2.enums import Side
from strategy_v2.events import canonical_sha256
from strategy_v2.r3_planning import TargetMode
from strategy_v2.r9_replay import (
    ReplayBar,
    ReplayCase,
    ReplayFeedKind,
    ReplayOutcomeReason,
    ReplayOutcomeStatus,
    evaluate_replay_case,
)


D = Decimal
BASE = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
DECISION = BASE + timedelta(minutes=5)
H = lambda value: canonical_sha256((value,)).lower()


def replay_case(side=Side.LONG, *, runner=False):
    return ReplayCase(
        "assembly-1", "ABCUSDT", "ABC-PERP-E1", side, DECISION,
        D("100"), D("98") if side is Side.LONG else D("102"),
        None if runner else (D("108") if side is Side.LONG else D("92")),
        TargetMode.FULL_POSITION_RUNNER if runner else TargetMode.STRUCTURAL_FINAL_TP,
        "entry-1", "stop-1", "target-1", "risk-1",
    )


def bar(
    index,
    *,
    open="100",
    high="101",
    low="99",
    close="100.5",
    start=None,
    symbol="ABCUSDT",
    epoch="ABC-PERP-E1",
    available_offset=1,
):
    start = start or DECISION + timedelta(minutes=5 * index)
    end = start + timedelta(minutes=5)
    return ReplayBar(
        f"bar-{index}", symbol, epoch, "5m",
        ReplayFeedKind.USD_M_FUTURES_TRADE_PRICE_FINAL_KLINE,
        start, end, end + timedelta(seconds=available_offset),
        D(open), D(high), D(low), D(close), H((index, start, open, high, low, close)),
    )


def evaluate(case, bars):
    return evaluate_replay_case(
        case=case,
        bars=bars,
        evaluation_at=max((item.available_at for item in bars), default=DECISION),
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_target_and_stop_outcomes_are_long_short_symmetric(side):
    case = replay_case(side)
    target_bar = (
        bar(0, high="108", low="99", close="107")
        if side is Side.LONG else bar(0, high="101", low="92", close="93")
    )
    target = evaluate(case, (target_bar,))
    assert target.status is ReplayOutcomeStatus.TARGET_HIT
    assert (target.gross_r_numerator, target.gross_r_denominator) == (4, 1)
    stop_bar = (
        bar(0, high="101", low="98", close="98.5")
        if side is Side.LONG else bar(0, high="102", low="99", close="101.5")
    )
    stopped = evaluate(case, (stop_bar,))
    assert stopped.status is ReplayOutcomeStatus.STOP_HIT
    assert (stopped.gross_r_numerator, stopped.gross_r_denominator) == (-1, 1)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_same_bar_dual_touch_is_censored_not_scored(side):
    case = replay_case(side)
    result = evaluate(case, (bar(0, high="108", low="92", close="100"),))
    assert result.status is ReplayOutcomeStatus.CENSORED
    assert result.reason is ReplayOutcomeReason.DUAL_TOUCH_AMBIGUOUS
    assert result.exit_price is None
    assert result.gross_r_numerator is None


def test_adverse_gap_uses_open_and_can_lose_more_than_one_r():
    case = replay_case()
    result = evaluate(case, (bar(0, open="97", high="99", low="96", close="98"),))
    assert result.status is ReplayOutcomeStatus.STOP_HIT
    assert result.exit_price == D("97")
    assert (result.gross_r_numerator, result.gross_r_denominator) == (-3, 2)


def test_favorable_target_gap_does_not_grant_price_improvement():
    case = replay_case()
    result = evaluate(case, (bar(0, open="110", high="111", low="109", close="110"),))
    assert result.status is ReplayOutcomeStatus.TARGET_HIT
    assert result.exit_price == D("108")
    assert (result.gross_r_numerator, result.gross_r_denominator) == (4, 1)


def test_open_path_has_no_fabricated_exit_or_pnl():
    case = replay_case()
    bars = (bar(0), bar(1, open="100.5", high="102", low="99.5", close="101"))
    result = evaluate(case, bars)
    assert result.status is ReplayOutcomeStatus.OPEN
    assert result.reason is None
    assert result.exit_price is None
    assert result.source_record_ids == tuple(item.record_id for item in bars)


def test_partial_entry_bar_is_rejected_to_avoid_pre_entry_path_contamination():
    case = replay_case()
    contaminated = bar(0, start=DECISION - timedelta(minutes=4))
    result = evaluate(case, (contaminated,))
    assert result.status is ReplayOutcomeStatus.UNRESOLVED
    assert result.reason is ReplayOutcomeReason.PRE_ENTRY_BAR


def test_gap_and_out_of_order_data_are_unresolved():
    case = replay_case()
    first = bar(0)
    gap = bar(2)
    result = evaluate(case, (first, gap))
    assert result.reason is ReplayOutcomeReason.DATA_GAP
    overlapping = bar(-1, start=DECISION)
    result = evaluate(case, (first, overlapping))
    assert result.reason is ReplayOutcomeReason.OUT_OF_ORDER


def test_missing_path_immediately_after_decision_is_a_gap_not_a_survivor():
    case = replay_case()
    late_first_bar = bar(1)
    result = evaluate(case, (late_first_bar,))
    assert result.status is ReplayOutcomeStatus.UNRESOLVED
    assert result.reason is ReplayOutcomeReason.DATA_GAP


def test_empty_duplicate_and_mixed_timeframe_paths_are_unresolved():
    case = replay_case()
    empty = evaluate_replay_case(case=case, bars=(), evaluation_at=DECISION)
    assert empty.reason is ReplayOutcomeReason.DATA_GAP
    first = bar(0)
    duplicate = replace(
        first,
        open_time=first.close_time,
        close_time=first.close_time + timedelta(minutes=5),
        available_at=first.close_time + timedelta(minutes=5, seconds=1),
    )
    result = evaluate(case, (first, duplicate))
    assert result.reason is ReplayOutcomeReason.DUPLICATE_BAR
    second = bar(1)
    mixed = replace(second, timeframe="1m")
    result = evaluate(case, (first, mixed))
    assert result.reason is ReplayOutcomeReason.TIMEFRAME_MISMATCH


def test_evaluation_cannot_precede_entry_decision():
    result = evaluate_replay_case(
        case=replay_case(), bars=(),
        evaluation_at=DECISION - timedelta(seconds=1),
    )
    assert result.reason is ReplayOutcomeReason.EVALUATION_BEFORE_DECISION


def test_future_or_wrong_instrument_evidence_is_unresolved():
    case = replay_case()
    future = bar(0)
    result = evaluate_replay_case(
        case=case, bars=(future,),
        evaluation_at=future.available_at - timedelta(seconds=1),
    )
    assert result.reason is ReplayOutcomeReason.FUTURE_EVIDENCE
    wrong = bar(0, epoch="ABC-PERP-E2")
    result = evaluate(case, (wrong,))
    assert result.reason is ReplayOutcomeReason.IDENTITY_MISMATCH


def test_runner_is_explicitly_unresolved_not_scored_like_fixed_target():
    result = evaluate(replay_case(runner=True), (bar(0),))
    assert result.status is ReplayOutcomeStatus.UNRESOLVED
    assert result.reason is ReplayOutcomeReason.RUNNER_POLICY_NOT_REPLAYABLE
    assert result.source_record_ids == ()


def test_replay_bar_rejects_malformed_ohlc_and_noncausal_times():
    with pytest.raises(ValueError, match="high below"):
        bar(0, open="100", high="99", low="98", close="100")
    valid = bar(0)
    with pytest.raises(ValueError, match="chronology"):
        replace(valid, available_at=valid.close_time - timedelta(seconds=1))


def test_outcome_identity_is_deterministic():
    case = replay_case()
    bars = (bar(0),)
    first = evaluate(case, bars)
    second = evaluate(case, bars)
    assert first == second
    assert first.outcome_id == second.outcome_id


def test_r9_has_no_exchange_order_or_runtime_surface():
    path = Path(__file__).parents[1] / "strategy_v2" / "r9_replay.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = ("binance", "exchange", "order", "main", "state_manager")
    assert not any(any(term in module.lower() for term in forbidden) for module in modules)
    assert "execution_authority: bool = field(default=False, init=False)" in source
