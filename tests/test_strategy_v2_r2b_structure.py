"""R2B pure structural-role and protection-contract tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_v2.causality import CausalTimes
from strategy_v2.enums import Side
from strategy_v2.events import canonical_sha256
from strategy_v2.market_data import ClosedBar
from strategy_v2.r2a_models import (
    ATRSourcePolicy,
    CausalQualificationResolution,
    CloseBreakCandidate,
    EvidenceResolutionReason,
    ThresholdStatus,
)
from strategy_v2.r2b_ledger import SlotClaimStatus, claim_market_slot
from strategy_v2.r2b_models import (
    FrozenStructuralEpoch,
    R2BOutcome,
    R2BReason,
    R2BVariant,
    SimulatedResearchGuard,
)
from strategy_v2.r2b_structure import (
    evaluate_continuation_bos,
    evaluate_initial_break,
    evaluate_protected_close_cross,
    resolve_continuation_role,
    resolve_initial_role,
    resolve_protection_candidates,
)
from strategy_v2.structure_models import (
    BreakDirection,
    ConfirmedPivot,
    PivotType,
    make_pivot_id,
)

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
D = Decimal
COMMIT = "8e08c8b8efc35834035f4dcf0f20c5f1550d6d3d"
SPEC_ID = canonical_sha256(("r1-structure-spec",))
R2A_HASH = canonical_sha256(("r2a-numeric-variant",)).lower()


def make_bar(index: int, close: str = "11", *, symbol: str = "ABCUSDT") -> ClosedBar:
    start = BASE + timedelta(minutes=index)
    value = D(close)
    return ClosedBar(
        f"bar-{index}", symbol, "1m", start, start + timedelta(minutes=1),
        start + timedelta(minutes=1), value, value + D("1"),
        value - D("1"), value, D("5"), True,
    )


def make_bars(*, short: bool = False) -> tuple[ClosedBar, ...]:
    closes = ["10", "11", "10", "10", "11", "12", "11", "11",
              "11", "11", "16", "18", "15", "14", "13", "12",
              "12", "16", "9", "11"]
    if short:
        closes = [str(D("30") - D(value)) for value in closes]
    return tuple(make_bar(index, close) for index, close in enumerate(closes))


def make_pivot(
    bars: tuple[ClosedBar, ...], index: int, pivot_type: PivotType, price: str,
) -> ConfirmedPivot:
    source, confirmation = bars[index], bars[index + 1]
    parent_ids = (source.bar_id, confirmation.bar_id)
    times = CausalTimes(source.close_time, confirmation.close_time,
                        confirmation.available_at)
    source_hash = canonical_sha256((source, confirmation))
    pivot_id = make_pivot_id(
        pivot_type=pivot_type, price=D(price), source_bar_id=source.bar_id,
        confirmation_bar_id=confirmation.bar_id, symbol=source.symbol,
        timeframe=source.timeframe, times=times,
        parent_evidence_ids=parent_ids, source_window_hash=source_hash,
        spec_id=SPEC_ID, rule_version="r1",
    )
    return ConfirmedPivot(
        pivot_id, pivot_type, D(price), source.bar_id, confirmation.bar_id,
        source.symbol, source.timeframe, times, parent_ids, source_hash,
        SPEC_ID, "r1",
    )


def make_pivots(bars: tuple[ClosedBar, ...], *, side: Side = Side.LONG):
    if side is Side.LONG:
        return (
            make_pivot(bars, 1, PivotType.HIGH, "12"),
            make_pivot(bars, 3, PivotType.LOW, "9"),
            make_pivot(bars, 5, PivotType.HIGH, "14"),
            make_pivot(bars, 7, PivotType.LOW, "10"),
        )
    return (
        make_pivot(bars, 1, PivotType.LOW, "18"),
        make_pivot(bars, 3, PivotType.HIGH, "21"),
        make_pivot(bars, 5, PivotType.LOW, "16"),
        make_pivot(bars, 7, PivotType.HIGH, "20"),
    )


def make_variant(*, symbol: str = "ABCUSDT") -> R2BVariant:
    return R2BVariant(
        source_commit=COMMIT,
        schema_version="R2B.1",
        rule_version="R2B_OFFLINE_RESEARCH_V1",
        variant_id="r2b-offline-v1",
        symbol=symbol,
        timeframe="1m",
        verified_feed_basis_id="TRADE_OHLCV",
        r2a_variant_hash=R2A_HASH,
        epoch_policy_id="FROZEN_OFFLINE_EPOCH_V1",
        initial_role_policy_id="INITIAL_ROLE_CONSECUTIVE_EPOCH_EXTREME_V1",
        first_cross_policy_id="FIRST_INITIAL_CROSS_V1",
        continuation_policy_id="CONTINUATION_REFERENCE_EARLIEST_UNCONTESTED_V1",
        counter_event_policy_id="COUNTER_EVENT_V1",
        protection_policy_id="POST_REFERENCE_RETRACE_UNIQUE_ONLY_V1",
        slot_policy_id="EVALUATION_FAMILY_SLOT_V1",
        semantic_snapshot_policy_id="R2A_SEMANTIC_SNAPSHOT_WHITELIST_V1",
    )


def make_epoch(bars: tuple[ClosedBar, ...], variant: R2BVariant, *, complete=True):
    return FrozenStructuralEpoch(
        symbol=variant.symbol,
        timeframe=variant.timeframe,
        start_at=bars[0].open_time,
        end_at=bars[-1].close_time,
        witness_available_at=bars[0].open_time,
        dataset_hash=canonical_sha256(bars),
        seed_coverage_hash=canonical_sha256((bars[:3], "seed")),
        complete_prefix=complete,
        variant_hash=variant.variant_hash,
    )


def make_numeric(
    bars: tuple[ClosedBar, ...], pivot: ConfirmedPivot, index: int,
    *, passed: bool, decision_at=None, r2a_hash=R2A_HASH,
) -> CloseBreakCandidate:
    current, previous = bars[index], bars[index - 1]
    direction = BreakDirection.UP if pivot.pivot_type is PivotType.HIGH else BreakDirection.DOWN
    status = ThresholdStatus.THRESHOLD_PASSED if passed else ThresholdStatus.THRESHOLD_NOT_PASSED
    reason = None if passed else EvidenceResolutionReason.NO_CLOSE_CROSS
    parents = (pivot.pivot_id, previous.bar_id, current.bar_id, "atr-1", "tick-1")
    if direction is BreakDirection.UP:
        threshold = (max(pivot.price + D("0.5"), (previous.close + current.close) / 2)
                     if passed else max(pivot.price + D("1"), current.close + D("1")))
    else:
        threshold = (min(pivot.price - D("0.5"), (previous.close + current.close) / 2)
                     if passed else min(pivot.price - D("1"), current.close - D("1")))
    return CloseBreakCandidate(
        status=status,
        reason=reason,
        direction=direction,
        symbol=current.symbol,
        timeframe=current.timeframe,
        previous_bar_id=previous.bar_id,
        break_bar_id=current.bar_id,
        broken_pivot_id=pivot.pivot_id,
        atr_evidence_id="atr-1",
        tick_evidence_id="tick-1",
        threshold_delta=abs(threshold - pivot.price),
        threshold_price=threshold,
        pivot_source_window_hash=pivot.source_window_hash,
        break_support_hash=canonical_sha256((previous, current, pivot)),
        variant_hash=r2a_hash,
        threshold_policy_id="threshold-v1",
        rule_version="r2a",
        source_commit=COMMIT,
        atr_source_policy=ATRSourcePolicy.PRE_BREAK_CLOSED,
        atr_method_id="SMA_TR",
        atr_period=14,
        atr_policy_id="ATR_SMA_TR_14_PRE_BREAK_V1",
        price_basis="TRADE_OHLCV",
        source_at=current.close_time,
        confirmed_at=current.close_time,
        available_at=current.available_at,
        decision_at=decision_at or current.available_at,
        parent_evidence_ids=parents,
        evidence_hash=canonical_sha256((parents, current.close_time)),
        qualification=CausalQualificationResolution(
            ThresholdStatus.UNRESOLVED,
            EvidenceResolutionReason.STRUCTURE_ROLE_NOT_PROVEN,
        ),
    )


def fixture(*, side=Side.LONG):
    bars = make_bars(short=side is Side.SHORT)
    pivots = make_pivots(bars, side=side)
    variant = make_variant()
    epoch = make_epoch(bars, variant)
    role = resolve_initial_role(
        pivots=pivots, bars=bars, candidate_bar_id="bar-11", side=side,
        epoch=epoch, variant=variant, decision_at=bars[11].available_at,
    )
    return bars, pivots, variant, epoch, role


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_initial_role_and_first_cross_confirm_symmetrically(side):
    bars, pivots, variant, epoch, role = fixture(side=side)
    assert role.role is not None and role.reason is None
    assert role.role.first_eligible_bar_id == "bar-10"
    result = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(make_numeric(bars, pivots[2], 10, passed=False),
                            make_numeric(bars, pivots[2], 11, passed=True)),
        candidate_bar_id="bar-11", epoch=epoch, variant=variant,
        decision_at=bars[11].available_at,
    )
    assert result.outcome is R2BOutcome.INITIAL_BREAK_CONFIRMED
    assert result.market_event_id
    assert result.slot.reference_id == pivots[2].pivot_id
    assert result.execution_authority is False


def test_threshold_not_passed_is_final_negative_not_unresolved():
    bars, pivots, variant, epoch, role = fixture()
    result = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(make_numeric(bars, pivots[2], 10, passed=False),),
        candidate_bar_id="bar-10", epoch=epoch, variant=variant,
        decision_at=bars[10].available_at,
    )
    assert result.outcome is R2BOutcome.FINAL_NO_CROSS
    assert result.reason is None and result.market_event_id


def test_missing_first_cross_history_fails_closed():
    bars, pivots, variant, epoch, role = fixture()
    result = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(make_numeric(bars, pivots[2], 11, passed=True),),
        candidate_bar_id="bar-11", epoch=epoch, variant=variant,
        decision_at=bars[11].available_at,
    )
    assert result.outcome is R2BOutcome.UNRESOLVED
    assert result.reason is R2BReason.INCOMPLETE_FIRST_CROSS_HISTORY
    assert result.market_event_id is None


def test_earlier_pass_makes_later_candidate_repeated_cross():
    bars, pivots, variant, epoch, role = fixture()
    result = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(make_numeric(bars, pivots[2], 10, passed=True),
                            make_numeric(bars, pivots[2], 11, passed=True)),
        candidate_bar_id="bar-11", epoch=epoch, variant=variant,
        decision_at=bars[11].available_at,
    )
    assert result.outcome is R2BOutcome.DISQUALIFIED
    assert result.reason is R2BReason.REPEATED_INITIAL_CROSS


def test_half_open_prior_breach_does_not_consume_first_eligible_bar():
    bars, pivots, variant, epoch, role = fixture()
    first = replace(bars[10], close=D("16"), high=D("17"), open=D("15"), low=D("14"))
    changed = (*bars[:10], first, *bars[11:])
    epoch = make_epoch(changed, variant)
    role = resolve_initial_role(
        pivots=pivots, bars=changed, candidate_bar_id="bar-10", side=Side.LONG,
        epoch=epoch, variant=variant, decision_at=changed[10].available_at,
    )
    result = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=changed,
        numeric_candidates=(make_numeric(changed, pivots[2], 10, passed=True),),
        candidate_bar_id="bar-10", epoch=epoch, variant=variant,
        decision_at=changed[10].available_at,
    )
    assert result.outcome is R2BOutcome.INITIAL_BREAK_CONFIRMED


def test_true_pre_eligibility_reference_breach_disqualifies():
    bars, pivots, variant, epoch, role = fixture()
    breached = replace(bars[8], close=D("15"), high=D("16"), open=D("14"), low=D("13"))
    changed = (*bars[:8], breached, *bars[9:])
    pivots = make_pivots(changed)
    epoch = make_epoch(changed, variant)
    role = resolve_initial_role(
        pivots=pivots, bars=changed, candidate_bar_id="bar-10", side=Side.LONG,
        epoch=epoch, variant=variant, decision_at=changed[10].available_at,
    )
    result = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=changed,
        numeric_candidates=(make_numeric(changed, pivots[2], 10, passed=False),),
        candidate_bar_id="bar-10", epoch=epoch, variant=variant,
        decision_at=changed[10].available_at,
    )
    assert result.reason is R2BReason.REFERENCE_PREVIOUSLY_BROKEN


def test_context_guard_breach_disqualifies_initial_break():
    bars, pivots, variant, epoch, role = fixture()
    broken = replace(bars[9], close=D("9"), high=D("10"), open=D("10"), low=D("8"))
    changed = (*bars[:9], broken, *bars[10:])
    epoch = make_epoch(changed, variant)
    role = resolve_initial_role(
        pivots=pivots, bars=changed, candidate_bar_id="bar-10", side=Side.LONG,
        epoch=epoch, variant=variant, decision_at=changed[10].available_at,
    )
    result = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=changed,
        numeric_candidates=(make_numeric(changed, pivots[2], 10, passed=False),),
        candidate_bar_id="bar-10", epoch=epoch, variant=variant,
        decision_at=changed[10].available_at,
    )
    assert result.reason is R2BReason.CONTEXT_GUARD_BROKEN


@pytest.mark.parametrize("mutation,reason", [
    ("variant", R2BReason.NUMERIC_VARIANT_MISMATCH),
    ("reference", R2BReason.NUMERIC_REFERENCE_MISMATCH),
    ("direction", R2BReason.NUMERIC_DIRECTION_MISMATCH),
])
def test_numeric_parent_must_match_role_and_variant(mutation, reason):
    bars, pivots, variant, epoch, role = fixture()
    numeric = make_numeric(bars, pivots[2], 10, passed=True)
    if mutation == "variant":
        numeric = replace(numeric, variant_hash="a" * 64)
    elif mutation == "reference":
        numeric = replace(numeric, broken_pivot_id=pivots[0].pivot_id)
    else:
        numeric = replace(numeric, direction=BreakDirection.DOWN)
    result = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(numeric,), candidate_bar_id="bar-10",
        epoch=epoch, variant=variant, decision_at=bars[10].available_at,
    )
    assert result.reason is reason


def test_market_event_identity_ignores_scanner_decision_time():
    bars, pivots, variant, epoch, role = fixture()
    numeric = make_numeric(bars, pivots[2], 10, passed=True)
    first = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(numeric,), candidate_bar_id="bar-10",
        epoch=epoch, variant=variant, decision_at=bars[10].available_at,
    )
    second = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(replace(numeric, decision_at=bars[12].available_at),),
        candidate_bar_id="bar-10", epoch=epoch, variant=variant,
        decision_at=bars[12].available_at,
    )
    assert first.market_event_id == second.market_event_id
    assert first.slot.market_slot_key == second.slot.market_slot_key
    assert first.evaluation_id != second.evaluation_id


def test_incomplete_epoch_and_nonconsecutive_role_are_unresolved():
    bars = make_bars()
    variant = make_variant()
    pivots = make_pivots(bars)
    incomplete = resolve_initial_role(
        pivots=pivots, bars=bars, candidate_bar_id="bar-11", side=Side.LONG,
        epoch=make_epoch(bars, variant, complete=False), variant=variant,
        decision_at=bars[11].available_at,
    )
    assert incomplete.reason is R2BReason.EPOCH_INCOMPLETE
    same_type = make_pivot(bars, 3, PivotType.HIGH, "13")
    unresolved = resolve_initial_role(
        pivots=(pivots[0], same_type, *pivots[2:]), bars=bars,
        candidate_bar_id="bar-11", side=Side.LONG,
        epoch=make_epoch(bars, variant), variant=variant,
        decision_at=bars[11].available_at,
    )
    assert unresolved.reason is R2BReason.STRUCTURE_ROLE_UNRESOLVED


def test_unique_only_protection_selects_one_fully_qualified_q():
    bars, pivots, variant, epoch, _ = fixture()
    ledger = resolve_protection_candidates(
        continuation_event_id="continuation-1", side=Side.LONG,
        reference_pivot=pivots[2], q_candidates=(pivots[3],),
        break_bar=bars[11], path_bars=bars[5:12], epoch=epoch,
        variant=variant, decision_at=bars[11].available_at,
    )
    assert ledger.outcome is R2BOutcome.PROMOTION_ELIGIBLE_CANDIDATE
    assert ledger.selected_pivot_id == pivots[3].pivot_id
    assert ledger.execution_authority is False


def test_unique_only_zero_and_multiple_are_unresolved():
    bars, pivots, variant, epoch, _ = fixture()
    breached_bar = replace(bars[9], close=D("9"), open=D("10"), high=D("10"), low=D("8"))
    path = (*bars[5:9], breached_bar, *bars[10:12])
    zero = resolve_protection_candidates(
        continuation_event_id="continuation-1", side=Side.LONG,
        reference_pivot=pivots[2], q_candidates=(pivots[3],),
        break_bar=bars[11], path_bars=path, epoch=epoch, variant=variant,
        decision_at=bars[11].available_at,
    )
    assert zero.reason is R2BReason.NO_QUALIFYING_PIVOT
    second_q = make_pivot(bars, 9, PivotType.LOW, "10.5")
    multiple = resolve_protection_candidates(
        continuation_event_id="continuation-1", side=Side.LONG,
        reference_pivot=pivots[2], q_candidates=(pivots[3], second_q),
        break_bar=bars[12], path_bars=bars[5:13], epoch=epoch,
        variant=variant, decision_at=bars[12].available_at,
    )
    assert multiple.reason is R2BReason.MULTIPLE_CANDIDATE_LEVELS
    assert multiple.selected_pivot_id is None


def continuation_fixture(*, side=Side.LONG, bars=None):
    if bars is None:
        bars = make_bars(short=side is Side.SHORT)
    pivots = make_pivots(bars, side=side)
    variant = make_variant()
    epoch = make_epoch(bars, variant)
    role = resolve_initial_role(
        pivots=pivots, bars=bars, candidate_bar_id="bar-10", side=side,
        epoch=epoch, variant=variant, decision_at=bars[10].available_at,
    )
    initial = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(make_numeric(bars, pivots[2], 10, passed=True),),
        candidate_bar_id="bar-10", epoch=epoch, variant=variant,
        decision_at=bars[10].available_at,
    )
    if side is Side.LONG:
        p = make_pivot(bars, 12, PivotType.HIGH, "15")
        q = make_pivot(bars, 14, PivotType.LOW, "11")
    else:
        p = make_pivot(bars, 12, PivotType.LOW, "15")
        q = make_pivot(bars, 14, PivotType.HIGH, "19")
    all_pivots = (*pivots, p, q)
    continuation = resolve_continuation_role(
        initial_evaluation=initial, initial_reference_pivot=pivots[2],
        initial_context_pivot=pivots[3],
        pivots=all_pivots, bars=bars, candidate_bar_id="bar-17", side=side,
        epoch=epoch, variant=variant, decision_at=bars[17].available_at,
    )
    return bars, pivots, p, q, all_pivots, variant, epoch, initial, continuation


def counter_history(bars, contextual_pivot, *, passed_index=None):
    candidates = tuple(
        make_numeric(bars, contextual_pivot, index, passed=index == passed_index)
        for index in range(11, 16)
    )
    return candidates, tuple(f"bar-{index}" for index in range(11, 16))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_continuation_bos_requires_independent_p_q_and_first_cross(side):
    bars, pivots, p, _, all_pivots, variant, epoch, _, continuation = continuation_fixture(side=side)
    assert continuation.role is not None
    assert continuation.role.first_eligible_bar_id == "bar-17"
    counters, coverage = counter_history(bars, pivots[3])
    result = evaluate_continuation_bos(
        role_resolution=continuation, pivots=all_pivots, bars=bars,
        numeric_candidates=(make_numeric(bars, p, 17, passed=True),),
        counter_candidates=counters, counter_coverage_bar_ids=coverage,
        candidate_bar_id="bar-17", epoch=epoch, variant=variant,
        decision_at=bars[17].available_at,
    )
    assert result.outcome is R2BOutcome.CONTINUATION_BOS_CONFIRMED
    assert result.market_event_id and result.execution_authority is False


def test_competing_continuation_reference_is_unresolved():
    bars, pivots, p, q, _, variant, epoch, initial, _ = continuation_fixture()
    competing = make_pivot(bars, 13, PivotType.HIGH, "16")
    result = resolve_continuation_role(
        initial_evaluation=initial, initial_reference_pivot=pivots[2],
        initial_context_pivot=pivots[3],
        pivots=(*pivots, p, competing, q), bars=bars,
        candidate_bar_id="bar-17", side=Side.LONG, epoch=epoch,
        variant=variant, decision_at=bars[17].available_at,
    )
    assert result.reason is R2BReason.COMPETING_REFERENCE


def test_counter_event_and_incomplete_counter_history_fail_closed():
    bars, pivots, p, _, all_pivots, variant, epoch, _, continuation = continuation_fixture()
    current = make_numeric(bars, p, 17, passed=True)
    incomplete = evaluate_continuation_bos(
        role_resolution=continuation, pivots=all_pivots, bars=bars,
        numeric_candidates=(current,), counter_candidates=(),
        counter_coverage_bar_ids=(), candidate_bar_id="bar-17",
        epoch=epoch, variant=variant, decision_at=bars[17].available_at,
    )
    assert incomplete.reason is R2BReason.INCOMPLETE_COUNTER_EVENT_HISTORY
    counter_bar = replace(bars[11], open=D("12"), high=D("13"),
                          low=D("8"), close=D("9"))
    changed = (*bars[:11], counter_bar, *bars[12:])
    (changed, pivots, p, _, all_pivots, variant, epoch, _,
     continuation) = continuation_fixture(bars=changed)
    counters, coverage = counter_history(changed, pivots[3], passed_index=11)
    rejected = evaluate_continuation_bos(
        role_resolution=continuation, pivots=all_pivots, bars=changed,
        numeric_candidates=(make_numeric(changed, p, 17, passed=True),),
        counter_candidates=counters, counter_coverage_bar_ids=coverage,
        candidate_bar_id="bar-17", epoch=epoch, variant=variant,
        decision_at=changed[17].available_at,
    )
    assert rejected.reason is R2BReason.COUNTER_EVENT
    assert rejected.outcome is R2BOutcome.DISQUALIFIED


def make_guard(bars, q, variant, epoch):
    return SimulatedResearchGuard(
        side=Side.LONG,
        level_pivot_id=q.pivot_id,
        level_price=q.price,
        continuation_event_id="continuation-1",
        market_active_from=bars[15].available_at,
        symbol=variant.symbol,
        timeframe=variant.timeframe,
        epoch_id=epoch.epoch_id,
        variant_hash=variant.variant_hash,
        protection_epoch_id="protection-epoch-1",
    )


def test_choch_requires_post_activation_previous_close_and_is_durable_ineligible():
    bars, _, _, q, _, variant, epoch, _, _ = continuation_fixture()
    guard = make_guard(bars, q, variant, epoch)
    result = evaluate_protected_close_cross(
        guard=guard, bars=bars, numeric_candidates=(), candidate_bar_id="bar-17",
        epoch=epoch, variant=variant, decision_at=bars[17].available_at,
    )
    assert result.evaluation.reason is R2BReason.NO_POST_ACTIVATION_PREVIOUS_CLOSE
    assert result.ineligibility is not None
    assert result.ineligibility.bar_id == "bar-17"
    assert result.guard_retired is False


def test_first_protected_cross_confirms_choch_without_thesis_invalidation():
    bars, _, _, q, _, variant, epoch, _, _ = continuation_fixture()
    guard = make_guard(bars, q, variant, epoch)
    numeric = make_numeric(bars, q, 18, passed=True)
    result = evaluate_protected_close_cross(
        guard=guard, bars=bars, numeric_candidates=(numeric,),
        candidate_bar_id="bar-18", epoch=epoch, variant=variant,
        decision_at=bars[18].available_at,
    )
    assert result.evaluation.outcome is R2BOutcome.CHOCH_CONFIRMED
    assert result.evaluation.market_event_id
    assert result.evaluation.slot.guard_id == guard.guard_id
    assert result.evaluation.slot.protection_epoch_id == guard.protection_epoch_id
    assert result.guard_retired is True
    assert not hasattr(result, "thesis_invalidated")


def test_later_protected_cross_cannot_replace_earlier_breach():
    bars, _, _, q, _, variant, epoch, _, _ = continuation_fixture()
    guard = make_guard(bars, q, variant, epoch)
    earlier = make_numeric(bars, q, 18, passed=True)
    later = make_numeric(bars, q, 19, passed=False)
    result = evaluate_protected_close_cross(
        guard=guard, bars=bars, numeric_candidates=(earlier, later),
        candidate_bar_id="bar-19", epoch=epoch, variant=variant,
        decision_at=bars[19].available_at,
    )
    assert result.evaluation.outcome is R2BOutcome.DISQUALIFIED
    assert result.evaluation.reason is R2BReason.GUARD_BREACH_PENDING_DISPOSITION
    assert result.guard_retired is True


def test_market_slot_claim_is_idempotent_and_conflicts_never_overwrite():
    bars, pivots, variant, epoch, role = fixture()
    final = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(make_numeric(bars, pivots[2], 10, passed=True),),
        candidate_bar_id="bar-10", epoch=epoch, variant=variant,
        decision_at=bars[10].available_at,
    )
    first = claim_market_slot((), final, claimed_at=bars[10].available_at)
    assert first.status is SlotClaimStatus.CLAIMED
    again = claim_market_slot(first.claims, final, claimed_at=bars[11].available_at)
    assert again.status is SlotClaimStatus.IDEMPOTENT
    assert again.claims == first.claims
    conflicting = replace(final, source_hash="a" * 64)
    conflict = claim_market_slot(first.claims, conflicting,
                                 claimed_at=bars[11].available_at)
    assert conflict.status is SlotClaimStatus.SOURCE_CONFLICT
    assert conflict.claims == first.claims
    assert conflict.accepted_claim_id is None
    assert conflict.conflicting_source_hashes == (final.source_hash, "a" * 64)


def test_unresolved_evaluation_never_claims_market_slot():
    bars, pivots, variant, epoch, role = fixture()
    unresolved = evaluate_initial_break(
        role_resolution=role, pivots=pivots, bars=bars,
        numeric_candidates=(), candidate_bar_id="bar-10", epoch=epoch,
        variant=variant, decision_at=bars[10].available_at,
    )
    transition = claim_market_slot((), unresolved,
                                   claimed_at=bars[10].available_at)
    assert transition.status is SlotClaimStatus.UNRESOLVED_IGNORED
    assert transition.claims == ()


def test_variant_and_ids_change_when_policy_changes():
    baseline = make_variant()
    changed = replace(baseline, protection_policy_id="POST_REFERENCE_RETRACE_OTHER")
    assert baseline.variant_hash != changed.variant_hash
    bars = make_bars()
    assert make_epoch(bars, baseline).epoch_id != make_epoch(bars, changed).epoch_id


def test_r2b_modules_have_no_runtime_or_execution_surface():
    package = Path(__file__).parents[1] / "strategy_v2"
    forbidden = ("exchange", "binance", "main", "telegram", "risk_manager",
                 "requests", "httpx", "aiohttp", "socket", "sqlite3")
    forbidden_calls = ("create_order", "cancel_order", "set_leverage", "connect",
                       "write_text", "write_bytes", "now", "utcnow")
    for name in ("r2b_models.py", "r2b_structure.py", "r2b_ledger.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not item.name.lower().startswith(forbidden) for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert not (node.module or "").lower().startswith(forbidden)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_calls
