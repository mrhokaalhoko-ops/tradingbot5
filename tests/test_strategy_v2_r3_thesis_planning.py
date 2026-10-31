"""Pure R3 thesis, stop, target, and re-entry tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from strategy_v2.causality import CausalTimes
from strategy_v2.enums import InvalidationRelation, InvalidationScope, Side
from strategy_v2.events import canonical_sha256
from strategy_v2.models import (
    ExitTarget,
    InvalidationLevel,
    RewardReference,
    ThesisId,
)
from strategy_v2.r2b_models import (
    MarketSlotIdentity,
    R2BEvaluationFamily,
    R2BOutcome,
    R2BStructuralEvaluation,
)
from strategy_v2.r3_planning import (
    CandidateStatus,
    PlanStatus,
    PlanningReason,
    StopPolicySpec,
    TargetMode,
    TargetPolicySpec,
    build_causal_target_plan,
    build_initial_stop_plan,
)
from strategy_v2.r3_thesis import (
    ReentryClassification,
    ResetEvidence,
    ThesisEffectPolicy,
    ThesisPolicySpec,
    ThesisSnapshot,
    ThesisStatus,
    ThesisTransitionReason,
    ThesisTransitionStatus,
    classify_reentry,
    create_thesis,
    derive_thesis_id,
    transition_thesis,
)

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
D = Decimal
COMMIT = "ea46a5c1547f55da91d066a2ee6a83af7d3baed3"
R2B_HASH = canonical_sha256(("r2b-variant",)).lower()
HTF_HASH = canonical_sha256(("4h", "1h", "closed-context"))


def thesis_policy():
    return ThesisPolicySpec(
        COMMIT, "R3.1", "R3_OFFLINE_RESEARCH_V1", "CAUSAL_THESIS_V1",
        R2B_HASH, "THESIS_LIFECYCLE_V1", "POST_CLOSE_CAUSAL_RESET_V1",
    )


def initial_break(*, side=Side.LONG, decision_at=BASE + timedelta(minutes=10)):
    slot = MarketSlotIdentity(
        R2BEvaluationFamily.INITIAL_BREAK, "ABCUSDT", "15m", "epoch-1",
        "pivot-ref", "bar-break", side, "TRADE_OHLCV", R2B_HASH,
    )
    return R2BStructuralEvaluation(
        R2BOutcome.INITIAL_BREAK_CONFIRMED, None, slot, "role-1",
        ("pivot-ref", "bar-break"), canonical_sha256((slot, "source")),
        decision_at,
    )


def thesis_identity_inputs(side=Side.LONG):
    event = initial_break(side=side)
    policy = thesis_policy()
    thesis_id = derive_thesis_id(
        symbol="ABCUSDT", side=side, setup_family="BOS_RETEST",
        epoch_id="epoch-1", initial_break_event_id=event.market_event_id,
        htf_context_snapshot_hash=HTF_HASH,
        causal_zone_ids=("zone-1",), policy=policy,
    )
    return event, policy, thesis_id


def invalidation(thesis_id, *, side=Side.LONG, price="95", scope=InvalidationScope.THESIS,
                 available_at=BASE + timedelta(minutes=9), level_id="inv-1"):
    relation = (InvalidationRelation.BREAK_BELOW if side is Side.LONG
                else InvalidationRelation.BREAK_ABOVE)
    times = CausalTimes(available_at - timedelta(minutes=2),
                        available_at - timedelta(minutes=1), available_at)
    return InvalidationLevel(
        level_id, scope, D(price), relation, side, "PROTECTED_STRUCTURE", "15m",
        times, thesis_id=thesis_id,
    )


def make_thesis(*, side=Side.LONG):
    event, policy, thesis_id = thesis_identity_inputs(side)
    price = "95" if side is Side.LONG else "105"
    return create_thesis(
        initial_break=event, symbol="ABCUSDT", side=side,
        setup_family="BOS_RETEST", epoch_id="epoch-1",
        htf_context_snapshot_hash=HTF_HASH, causal_zone_ids=("zone-1",),
        invalidation=invalidation(thesis_id, side=side, price=price), policy=policy,
        decision_at=BASE + timedelta(minutes=11),
    )


def test_thesis_identity_is_stable_across_registration_time_and_proposals():
    first = make_thesis()
    event, policy, thesis_id = thesis_identity_inputs()
    later = create_thesis(
        initial_break=replace(event, decision_at=BASE + timedelta(minutes=20)),
        symbol="ABCUSDT", side=Side.LONG, setup_family="BOS_RETEST",
        epoch_id="epoch-1", htf_context_snapshot_hash=HTF_HASH,
        causal_zone_ids=("zone-1",),
        invalidation=invalidation(thesis_id), policy=policy,
        decision_at=BASE + timedelta(minutes=21),
    )
    assert first.identity.thesis_id == later.identity.thesis_id
    assert first.identity.initial_break_event_id == later.identity.initial_break_event_id
    assert first.snapshot_id != later.snapshot_id


def test_thesis_requires_explicit_causal_invalidation_bound_to_identity():
    event, policy, thesis_id = thesis_identity_inputs()
    with pytest.raises(ValueError, match="causal thesis invalidation"):
        create_thesis(
            initial_break=event, symbol="ABCUSDT", side=Side.LONG,
            setup_family="BOS_RETEST", epoch_id="epoch-1",
            htf_context_snapshot_hash=HTF_HASH, causal_zone_ids=("zone-1",),
            invalidation=invalidation(ThesisId("wrong")), policy=policy,
            decision_at=BASE + timedelta(minutes=11),
        )
    with pytest.raises(ValueError, match="causal thesis invalidation"):
        create_thesis(
            initial_break=event, symbol="ABCUSDT", side=Side.LONG,
            setup_family="BOS_RETEST", epoch_id="epoch-1",
            htf_context_snapshot_hash=HTF_HASH, causal_zone_ids=("zone-1",),
            invalidation=invalidation(thesis_id, scope=InvalidationScope.SETUP),
            policy=policy, decision_at=BASE + timedelta(minutes=11),
        )


def test_thesis_rejects_initial_break_from_different_market_identity():
    event, policy, _ = thesis_identity_inputs()
    mismatched_id = derive_thesis_id(
        symbol="OTHERUSDT", side=Side.LONG, setup_family="BOS_RETEST",
        epoch_id="epoch-1", initial_break_event_id=event.market_event_id,
        htf_context_snapshot_hash=HTF_HASH, causal_zone_ids=("zone-1",),
        policy=policy,
    )
    with pytest.raises(ValueError, match="provenance does not match"):
        create_thesis(
            initial_break=event, symbol="OTHERUSDT", side=Side.LONG,
            setup_family="BOS_RETEST", epoch_id="epoch-1",
            htf_context_snapshot_hash=HTF_HASH, causal_zone_ids=("zone-1",),
            invalidation=invalidation(mismatched_id), policy=policy,
            decision_at=BASE + timedelta(minutes=11),
        )


def advance(snapshot, status, minute):
    return transition_thesis(
        snapshot, target=status, event_at=BASE + timedelta(minutes=minute),
        decision_at=BASE + timedelta(minutes=minute),
        evidence_ids=(f"evidence-{minute}",), reason_code=f"TO_{status.value}",
    )


def test_thesis_lifecycle_is_typed_idempotent_and_terminal():
    snapshot = make_thesis()
    for status, minute in (
        (ThesisStatus.QUALIFIED, 12), (ThesisStatus.WAIT_TRIGGER, 13),
        (ThesisStatus.PROPOSED, 14), (ThesisStatus.ENTRY_PENDING, 15),
        (ThesisStatus.ACTIVE, 16), (ThesisStatus.CLOSED, 17),
    ):
        result = advance(snapshot, status, minute)
        assert result.status is ThesisTransitionStatus.APPLIED
        snapshot = result.snapshot
    duplicate = advance(snapshot, ThesisStatus.CLOSED, 18)
    assert duplicate.status is ThesisTransitionStatus.IDEMPOTENT
    rejected = advance(snapshot, ThesisStatus.ACTIVE, 19)
    assert rejected.status is ThesisTransitionStatus.REJECTED
    assert rejected.reason is ThesisTransitionReason.TERMINAL_STATE


def test_direct_snapshot_construction_rejects_discontinuous_event_chain():
    original = make_thesis()
    qualified = advance(original, ThesisStatus.QUALIFIED, 12).snapshot
    forged = replace(qualified.events[-1], from_status=ThesisStatus.WAIT_TRIGGER)
    with pytest.raises(ValueError, match="discontinuous"):
        ThesisSnapshot(
            qualified.identity, qualified.status, qualified.created_at,
            qualified.last_event_at, (qualified.events[0], forged),
        )


def test_invalid_transition_and_future_evidence_fail_closed():
    snapshot = make_thesis()
    invalid = advance(snapshot, ThesisStatus.ACTIVE, 12)
    assert invalid.reason is ThesisTransitionReason.INVALID_TRANSITION
    future = transition_thesis(
        snapshot, target=ThesisStatus.QUALIFIED,
        event_at=BASE + timedelta(minutes=13),
        decision_at=BASE + timedelta(minutes=12), evidence_ids=("future",),
        reason_code="FUTURE",
    )
    assert future.reason is ThesisTransitionReason.FUTURE_EVIDENCE


def test_choch_does_not_automatically_invalidate_thesis():
    snapshot = make_thesis()
    result = transition_thesis(
        snapshot, target=ThesisStatus.INVALIDATED,
        event_at=BASE + timedelta(minutes=12),
        decision_at=BASE + timedelta(minutes=12),
        evidence_ids=("choch-market-event",), reason_code="CHOCH_ONLY",
        effect_policy=ThesisEffectPolicy.NONE,
    )
    assert result.status is ThesisTransitionStatus.REJECTED
    assert result.reason is ThesisTransitionReason.CHOCH_HAS_NO_AUTOMATIC_THESIS_EFFECT
    assert result.snapshot.status is ThesisStatus.OBSERVED


def test_explicit_thesis_invalidation_requires_separate_effect_event():
    snapshot = make_thesis()
    result = transition_thesis(
        snapshot, target=ThesisStatus.INVALIDATED,
        event_at=BASE + timedelta(minutes=12),
        decision_at=BASE + timedelta(minutes=12),
        evidence_ids=("macro-break", "invalidation-level"),
        reason_code="EXPLICIT_MACRO_INVALIDATION",
        effect_policy=ThesisEffectPolicy.EXPLICIT_INVALIDATION,
        explicit_invalidation_event_id="thesis-invalidation-1",
    )
    assert result.status is ThesisTransitionStatus.APPLIED
    assert result.snapshot.status is ThesisStatus.INVALIDATED


@pytest.mark.parametrize("side,entry,level,expected", [
    (Side.LONG, "100", "95", "93.5"),
    (Side.SHORT, "100", "105", "106.5"),
])
def test_stop_is_outside_causal_invalidation_with_explicit_buffer(side, entry, level, expected):
    thesis_id = ThesisId("thesis-1")
    policy = StopPolicySpec(
        COMMIT, "STRUCTURAL_INITIAL_STOP_R3", "r3", D("0.5"), 2,
        D("1"), "EXACT_FRACTION_OUTWARD_GRID_V1",
    )
    plan = build_initial_stop_plan(
        thesis_id=thesis_id, side=side, entry_price=D(entry),
        invalidations=(invalidation(thesis_id, side=side, price=level),),
        required_scope=InvalidationScope.THESIS, selected_level_id=None,
        atr=D("3"), tick_size=D("0.1"), spread=D("0.2"), policy=policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert plan.status is PlanStatus.READY
    assert plan.buffer == D("1.5")
    assert plan.final_stop == D(expected)
    assert plan.execution_authority is False


def test_stop_enumerates_every_candidate_and_never_silently_picks_between_many():
    thesis_id = ThesisId("thesis-1")
    levels = (invalidation(thesis_id, level_id="inv-1", price="95"),
              invalidation(thesis_id, level_id="inv-2", price="92"))
    policy = StopPolicySpec(COMMIT, "stop", "r3", D("0.3"), 2, D("1"), "exact")
    unresolved = build_initial_stop_plan(
        thesis_id=thesis_id, side=Side.LONG, entry_price=D("100"),
        invalidations=levels, required_scope=InvalidationScope.THESIS,
        selected_level_id=None, atr=D("2"), tick_size=D("0.1"),
        spread=D("0.1"), policy=policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert unresolved.reason is PlanningReason.MULTIPLE_CAUSAL_INVALIDATIONS
    assert len(unresolved.assessments) == 2
    selected = build_initial_stop_plan(
        thesis_id=thesis_id, side=Side.LONG, entry_price=D("100"),
        invalidations=levels, required_scope=InvalidationScope.THESIS,
        selected_level_id="inv-2", atr=D("2"), tick_size=D("0.1"),
        spread=D("0.1"), policy=policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert selected.status is PlanStatus.READY
    assert selected.selected_level_id == "inv-2"


def test_stop_rejects_future_wrong_scope_and_inward_geometry():
    thesis_id = ThesisId("thesis-1")
    policy = StopPolicySpec(COMMIT, "stop", "r3", D("0.3"), 2, D("1"), "exact")
    levels = (
        invalidation(thesis_id, level_id="future", available_at=BASE + timedelta(minutes=20)),
        invalidation(thesis_id, level_id="setup", scope=InvalidationScope.SETUP),
        invalidation(thesis_id, level_id="wrong-side", price="101"),
    )
    plan = build_initial_stop_plan(
        thesis_id=thesis_id, side=Side.LONG, entry_price=D("100"),
        invalidations=levels, required_scope=InvalidationScope.THESIS,
        selected_level_id=None, atr=D("2"), tick_size=D("0.1"),
        spread=D("0.1"), policy=policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert plan.reason is PlanningReason.NO_CAUSAL_INVALIDATION
    reasons = {item.level.level_id: item.reason for item in plan.assessments}
    assert reasons == {
        "future": PlanningReason.FUTURE_INVALIDATION,
        "setup": PlanningReason.SELECTED_INVALIDATION_REJECTED,
        "wrong-side": PlanningReason.INVALID_STOP_GEOMETRY,
    }


def target(target_id, price, *, side=Side.LONG, available=BASE + timedelta(minutes=9)):
    times = CausalTimes(available - timedelta(minutes=2),
                        available - timedelta(minutes=1), available)
    return ExitTarget(target_id, D(price), side, f"level-{target_id}", times)


def reward(price="110", *, side=Side.LONG, available=BASE + timedelta(minutes=9)):
    times = CausalTimes(available - timedelta(minutes=2),
                        available - timedelta(minutes=1), available)
    return RewardReference("reward-1", D(price), side, "liquidity-1", times)


def test_structural_target_uses_causal_price_not_fixed_rr_geometry():
    policy = TargetPolicySpec(
        COMMIT, "STRUCTURAL_TARGET_V1", "r3", TargetMode.STRUCTURAL_FINAL_TP,
        D("1"), (), 8, "EXACT_FRACTION_FLOOR_RR_V1",
    )
    plan = build_causal_target_plan(
        side=Side.LONG, entry_price=D("100"), stop_price=D("95"),
        targets=(target("market-resistance", "112"),),
        selected_target_id=None, reward_reference=reward("112"), policy=policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert plan.status is PlanStatus.READY
    assert plan.selected_target.price == D("112")
    assert plan.gross_rr == D("2.4")
    assert plan.selected_target.price != D("120")


def test_multiple_targets_require_explicit_policy_choice_and_low_rr_rejects():
    policy = TargetPolicySpec(
        COMMIT, "STRUCTURAL_TARGET_V1", "r3", TargetMode.STRUCTURAL_FINAL_TP,
        D("2"), (), 8, "EXACT_FRACTION_FLOOR_RR_V1",
    )
    targets = (target("near", "106"), target("far", "114"))
    ambiguous = build_causal_target_plan(
        side=Side.LONG, entry_price=D("100"), stop_price=D("95"), targets=targets,
        selected_target_id=None, reward_reference=reward("114"), policy=policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert ambiguous.reason is PlanningReason.MULTIPLE_CAUSAL_TARGETS
    near = build_causal_target_plan(
        side=Side.LONG, entry_price=D("100"), stop_price=D("95"), targets=targets,
        selected_target_id="near", reward_reference=reward("114"), policy=policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert near.status is PlanStatus.REJECTED
    assert near.reason is PlanningReason.NET_RR_BELOW_MINIMUM


def test_runner_has_no_fabricated_price_target_and_needs_explicit_exit_events():
    policy = TargetPolicySpec(
        COMMIT, "STRUCTURAL_RUNNER_V1", "r3", TargetMode.FULL_POSITION_RUNNER,
        D("0"), ("CHOCH_CONFIRMED", "THESIS_INVALIDATED"), 8,
        "EXACT_FRACTION_FLOOR_RR_V1",
    )
    plan = build_causal_target_plan(
        side=Side.LONG, entry_price=D("100"), stop_price=D("95"), targets=(),
        selected_target_id=None, reward_reference=reward("110"), policy=policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert plan.status is PlanStatus.READY
    assert plan.selected_target is None and plan.gross_rr is None
    assert plan.runner_exit_event_types == ("CHOCH_CONFIRMED", "THESIS_INVALIDATED")


def test_target_future_or_wrong_side_is_not_usable():
    policy = TargetPolicySpec(
        COMMIT, "STRUCTURAL_TARGET_V1", "r3", TargetMode.STRUCTURAL_FINAL_TP,
        D("0"), (), 8, "EXACT_FRACTION_FLOOR_RR_V1",
    )
    candidates = (
        target("future", "112", available=BASE + timedelta(minutes=20)),
        target("wrong-side", "90"),
    )
    plan = build_causal_target_plan(
        side=Side.LONG, entry_price=D("100"), stop_price=D("95"),
        targets=candidates, selected_target_id=None, reward_reference=reward(),
        policy=policy, decision_at=BASE + timedelta(minutes=10),
    )
    assert plan.reason is PlanningReason.NO_CAUSAL_TARGET
    assert {item.reason for item in plan.assessments} == {
        PlanningReason.FUTURE_TARGET, PlanningReason.TARGET_WRONG_SIDE,
    }


def test_stop_numeric_result_is_independent_of_ambient_decimal_context():
    thesis_id = ThesisId("thesis-1")
    policy = StopPolicySpec(COMMIT, "stop", "r3", D("0.333"), 2, D("1.5"), "exact")
    observed = []
    for precision in (6, 9, 28, 50):
        with localcontext() as context:
            context.prec = precision
            plan = build_initial_stop_plan(
                thesis_id=thesis_id, side=Side.LONG, entry_price=D("100"),
                invalidations=(invalidation(thesis_id, price="95.03"),),
                required_scope=InvalidationScope.THESIS, selected_level_id=None,
                atr=D("3.7"), tick_size=D("0.01"), spread=D("0.2"),
                policy=policy, decision_at=BASE + timedelta(minutes=10),
            )
        observed.append((plan.buffer, plan.final_stop, plan.risk_distance, plan.plan_id))
    assert len(set(observed)) == 1


def test_target_numeric_result_is_independent_of_ambient_decimal_context():
    policy = TargetPolicySpec(
        COMMIT, "target", "r3", TargetMode.STRUCTURAL_FINAL_TP,
        D("2.333333333333333333"), (), 12, "EXACT_FRACTION_FLOOR_RR_V1",
    )
    observed = []
    for precision in (6, 9, 28, 50):
        with localcontext() as context:
            context.prec = precision
            plan = build_causal_target_plan(
                side=Side.LONG, entry_price=D("100.123456789123456789"),
                stop_price=D("97.987654321987654321"),
                targets=(target("causal", "105.234567891234567891"),),
                selected_target_id=None,
                reward_reference=reward("105.234567891234567891"),
                policy=policy, decision_at=BASE + timedelta(minutes=10),
            )
        observed.append((plan.status, plan.reason, plan.gross_rr, plan.plan_id))
    assert len(set(observed)) == 1


def test_numeric_values_outside_explicit_domain_fail_closed():
    thesis_id = ThesisId("thesis-1")
    stop_policy = StopPolicySpec(COMMIT, "stop", "r3", D("0.3"), 2, D("1"), "exact")
    stop = build_initial_stop_plan(
        thesis_id=thesis_id, side=Side.LONG, entry_price=D("1E+1000"),
        invalidations=(invalidation(thesis_id, price="95"),),
        required_scope=InvalidationScope.THESIS, selected_level_id=None,
        atr=D("2"), tick_size=D("0.1"), spread=D("0.1"),
        policy=stop_policy, decision_at=BASE + timedelta(minutes=10),
    )
    assert stop.status is PlanStatus.UNRESOLVED
    assert stop.reason is PlanningReason.NUMERIC_UNCOMPUTABLE

    target_policy = TargetPolicySpec(
        COMMIT, "target", "r3", TargetMode.STRUCTURAL_FINAL_TP,
        D("1"), (), 8, "EXACT_FRACTION_FLOOR_RR_V1",
    )
    target_plan = build_causal_target_plan(
        side=Side.LONG, entry_price=D("1E+1000"), stop_price=D("95"),
        targets=(target("causal", "1.1E+1000"),), selected_target_id=None,
        reward_reference=reward("1.1E+1000"), policy=target_policy,
        decision_at=BASE + timedelta(minutes=10),
    )
    assert target_plan.status is PlanStatus.UNRESOLVED
    assert target_plan.reason is PlanningReason.NUMERIC_UNCOMPUTABLE


@pytest.mark.parametrize("same,context,reset_minute,expected", [
    (False, True, 12, ReentryClassification.NEW_THESIS),
    (True, True, 12, ReentryClassification.SAME_THESIS_VALID_RESET),
    (True, True, 10, ReentryClassification.SAME_THESIS_NO_RESET),
    (True, False, 12, ReentryClassification.CONTEXT_INVALIDATED),
])
def test_reentry_requires_post_close_causal_reset(same, context, reset_minute, expected):
    prior = ThesisId("thesis-1")
    candidate = prior if same else ThesisId("thesis-2")
    reset = ResetEvidence(
        "reset-1", prior, BASE + timedelta(minutes=reset_minute),
        BASE + timedelta(minutes=reset_minute), ("structure-reset", "new-zone"),
        context,
    )
    result = classify_reentry(
        prior_thesis_id=prior, candidate_thesis_id=candidate,
        prior_close_at=BASE + timedelta(minutes=10),
        decision_at=BASE + timedelta(minutes=20), reset=reset,
    )
    assert result is expected


def test_r3_modules_have_no_runtime_or_exchange_surface():
    package = Path(__file__).parents[1] / "strategy_v2"
    forbidden = ("exchange", "binance", "main", "telegram", "risk_manager",
                 "requests", "httpx", "aiohttp", "socket", "sqlite3")
    forbidden_calls = ("create_order", "cancel_order", "set_leverage", "connect",
                       "write_text", "write_bytes", "now", "utcnow")
    for name in ("r3_thesis.py", "r3_planning.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not item.name.lower().startswith(forbidden) for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert not (node.module or "").lower().startswith(forbidden)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_calls
