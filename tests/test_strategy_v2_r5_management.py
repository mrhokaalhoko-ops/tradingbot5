"""Pure R5 structural management tests."""

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from strategy_v2.causality import CausalTimes
from strategy_v2.enums import InvalidationRelation, InvalidationScope, Side
from strategy_v2.events import canonical_sha256
from strategy_v2.models import InvalidationLevel, ThesisId
from strategy_v2.r2b_models import (
    MarketSlotIdentity,
    R2BEvaluationFamily,
    R2BOutcome,
    R2BStructuralEvaluation,
)
from strategy_v2.r3_planning import StopPolicySpec
from strategy_v2.r3_thesis import (
    ThesisEffectPolicy,
    ThesisPolicySpec,
    ThesisStatus,
    create_thesis,
    derive_thesis_id,
    transition_thesis,
)
from strategy_v2.r5_management import (
    ManagementAction,
    ManagementPolicySpec,
    ManagementReason,
    MarketPriceObservation,
    PositionProtectionState,
    ProtectedStructureEvidence,
    evaluate_structural_management,
)


D = Decimal
BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
COMMIT = "d454c32bb3b875d741a5440e5db080b3cb910892"
VARIANT = canonical_sha256(("r2b-variant",)).lower()
HTF_HASH = canonical_sha256(("4h", "1h", "closed-context")).lower()


def management_policy():
    return ManagementPolicySpec(
        COMMIT, "STRUCTURAL_RATCHET_V1", "r5",
        "CONFIRMED_PROTECTED_STRUCTURE_AND_CONTINUATION_V1",
        "CHOCH_REVIEW_ONLY_V1", "EXACT_FRACTION_OUTWARD_GRID_V1",
    )


def stop_policy():
    return StopPolicySpec(
        COMMIT, "RATCHET_BUFFER_V1", "r5", D("0.5"), 2, D("1"),
        "EXACT_FRACTION_OUTWARD_GRID_V1",
    )


def position(side=Side.LONG, current=None, thesis_id=None):
    original = D("95") if side is Side.LONG else D("105")
    return PositionProtectionState(
        "position-1", "ABCUSDT", "ABC-PERP-E1",
        thesis_id or ThesisId("placeholder"),
        side, D("100"), original, current or original, 0,
        BASE + timedelta(minutes=19), ("fill-1", "confirmed-stop-1"),
    )


def market(price="110"):
    at = BASE + timedelta(minutes=20)
    return MarketPriceObservation(
        "ABCUSDT", "ABC-PERP-E1", "TRADE_LAST", D(price), at, at,
        canonical_sha256(("quote", price, at)).lower(),
    )


def structural_event(family, outcome, side, bar_id):
    slot = MarketSlotIdentity(
        family, "ABCUSDT", "15m", "epoch-1", "reference-1", bar_id,
        side, "TRADE_OHLCV", VARIANT,
        guard_id="guard-1" if family is R2BEvaluationFamily.PROTECTED_CLOSE_CROSS else None,
        protection_epoch_id="protect-1" if family is R2BEvaluationFamily.PROTECTED_CLOSE_CROSS else None,
    )
    return R2BStructuralEvaluation(
        outcome, None, slot, "role-1", ("source-1",),
        canonical_sha256((slot, outcome)).lower(),
        BASE + timedelta(minutes=18),
    )


def make_thesis(side=Side.LONG):
    event = structural_event(
        R2BEvaluationFamily.INITIAL_BREAK,
        R2BOutcome.INITIAL_BREAK_CONFIRMED, side, "bar-initial",
    )
    thesis_policy = ThesisPolicySpec(
        COMMIT, "R3.1", "R3_OFFLINE_RESEARCH_V1", "CAUSAL_THESIS_V1",
        VARIANT, "THESIS_LIFECYCLE_V1", "POST_CLOSE_CAUSAL_RESET_V1",
    )
    thesis_id = derive_thesis_id(
        symbol="ABCUSDT", side=side, setup_family="BOS_RETEST",
        epoch_id="epoch-1", initial_break_event_id=event.market_event_id,
        htf_context_snapshot_hash=HTF_HASH, causal_zone_ids=("zone-1",),
        policy=thesis_policy,
    )
    at = BASE + timedelta(minutes=9)
    invalidation = InvalidationLevel(
        "initial-invalidation", InvalidationScope.THESIS,
        D("95") if side is Side.LONG else D("105"),
        InvalidationRelation.BREAK_BELOW if side is Side.LONG
        else InvalidationRelation.BREAK_ABOVE,
        side, "INITIAL_PROTECTED_STRUCTURE", "15m",
        CausalTimes(at - timedelta(minutes=2), at - timedelta(minutes=1), at),
        thesis_id=thesis_id,
    )
    return create_thesis(
        initial_break=event, symbol="ABCUSDT", side=side,
        setup_family="BOS_RETEST", epoch_id="epoch-1",
        htf_context_snapshot_hash=HTF_HASH, causal_zone_ids=("zone-1",),
        invalidation=invalidation, policy=thesis_policy,
        decision_at=BASE + timedelta(minutes=19),
    )
def protection(thesis_id, side=Side.LONG, level_price="105", available_minute=17):
    at = BASE + timedelta(minutes=available_minute)
    relation = (InvalidationRelation.BREAK_BELOW if side is Side.LONG
                else InvalidationRelation.BREAK_ABOVE)
    level = InvalidationLevel(
        "protected-new", InvalidationScope.THESIS, D(level_price), relation,
        side, "R2B_PROTECTED_STRUCTURE", "15m",
        CausalTimes(at - timedelta(minutes=2), at - timedelta(minutes=1), at),
        thesis_id=thesis_id,
    )
    continuation = structural_event(
        R2BEvaluationFamily.CONTINUATION_BOS,
        R2BOutcome.CONTINUATION_BOS_CONFIRMED, side, "bar-continuation",
    )
    return ProtectedStructureEvidence(level, continuation)


def inputs(side=Side.LONG, **overrides):
    thesis = make_thesis(side=side)
    pos = position(side, thesis_id=thesis.identity.thesis_id)
    values = dict(
        position=pos, thesis=thesis,
        market=market("110" if side is Side.LONG else "90"),
        protection=None, adverse_choch=None, atr=D("2"), tick_size=D("0.1"),
        spread=D("0.2"), stop_policy=stop_policy(),
        policy=management_policy(), decision_at=BASE + timedelta(minutes=20),
    )
    values.update(overrides)
    return values


def test_no_new_structure_holds_without_r_threshold_shortcut():
    result = evaluate_structural_management(**inputs())
    assert result.action is ManagementAction.HOLD
    assert result.reason is ManagementReason.NO_NEW_PROTECTED_STRUCTURE
    assert result.proposed_stop is None


def test_long_and_short_structural_ratchets_are_symmetric():
    long_values = inputs()
    long_values["protection"] = protection(
        long_values["thesis"].identity.thesis_id, level_price="105",
    )
    long = evaluate_structural_management(**long_values)
    assert long.action is ManagementAction.RATCHET_CANDIDATE
    assert D("95") < long.proposed_stop < D("105")

    short_values = inputs(Side.SHORT)
    short_values["protection"] = protection(
        short_values["thesis"].identity.thesis_id, Side.SHORT, "95",
    )
    short = evaluate_structural_management(**short_values)
    assert short.action is ManagementAction.RATCHET_CANDIDATE
    assert D("95") < short.proposed_stop < D("105")


def test_stop_never_regresses_even_with_valid_new_structure():
    values = inputs()
    values["position"] = PositionProtectionState(
        "position-1", "ABCUSDT", "ABC-PERP-E1",
        values["thesis"].identity.thesis_id, Side.LONG, D("100"), D("95"),
        D("106"), 2, BASE + timedelta(minutes=19),
        ("fill-1", "confirmed-stop-2"),
    )
    values["protection"] = protection(values["thesis"].identity.thesis_id)
    result = evaluate_structural_management(**values)
    assert result.action is ManagementAction.HOLD
    assert result.reason is ManagementReason.STOP_NOT_IMPROVED


def test_future_structure_and_market_evidence_fail_closed():
    values = inputs()
    values["protection"] = protection(
        values["thesis"].identity.thesis_id, available_minute=25,
    )
    future_structure = evaluate_structural_management(**values)
    assert future_structure.action is ManagementAction.UNRESOLVED
    assert future_structure.reason is ManagementReason.STRUCTURE_EVIDENCE_FUTURE

    values = inputs()
    values["market"] = MarketPriceObservation(
        "ABCUSDT", "ABC-PERP-E1", "TRADE_LAST", D("110"),
        BASE + timedelta(minutes=21), BASE + timedelta(minutes=21),
        canonical_sha256(("future-quote",)).lower(),
    )
    future_market = evaluate_structural_management(**values)
    assert future_market.reason is ManagementReason.MARKET_OBSERVATION_FUTURE


def test_future_position_or_thesis_state_is_not_used():
    values = inputs()
    values["position"] = PositionProtectionState(
        "position-1", "ABCUSDT", "ABC-PERP-E1",
        values["thesis"].identity.thesis_id, Side.LONG, D("100"), D("95"),
        D("95"), 0, BASE + timedelta(minutes=21),
        ("fill-1", "confirmed-stop-1"),
    )
    assert evaluate_structural_management(**values).reason is ManagementReason.POSITION_EVIDENCE_FUTURE

    values = inputs()
    future_thesis = transition_thesis(
        values["thesis"], target=ThesisStatus.QUALIFIED,
        event_at=BASE + timedelta(minutes=21),
        decision_at=BASE + timedelta(minutes=21),
        evidence_ids=("future-qualification",), reason_code="FUTURE",
    ).snapshot
    values["thesis"] = future_thesis
    assert evaluate_structural_management(**values).reason is ManagementReason.THESIS_EVIDENCE_FUTURE


def test_market_already_beyond_effective_stop_requires_review_not_ratchet():
    values = inputs()
    values["market"] = market("94")
    result = evaluate_structural_management(**values)
    assert result.action is ManagementAction.REVIEW_REQUIRED
    assert result.reason is ManagementReason.CURRENT_PRICE_BEYOND_EFFECTIVE_STOP


def test_structure_identity_mismatch_is_unresolved():
    values = inputs()
    values["protection"] = protection(ThesisId("other-thesis"))
    result = evaluate_structural_management(**values)
    assert result.action is ManagementAction.UNRESOLVED
    assert result.reason is ManagementReason.STRUCTURE_IDENTITY_MISMATCH


def test_adverse_choch_requests_review_but_does_not_auto_invalidate_or_exit():
    values = inputs()
    values["adverse_choch"] = structural_event(
        R2BEvaluationFamily.PROTECTED_CLOSE_CROSS,
        R2BOutcome.CHOCH_CONFIRMED, Side.SHORT, "bar-choch",
    )
    result = evaluate_structural_management(**values)
    assert result.action is ManagementAction.REVIEW_REQUIRED
    assert result.reason is ManagementReason.CHOCH_NO_AUTOMATIC_THESIS_EFFECT
    assert values["thesis"].status is ThesisStatus.OBSERVED


def test_same_direction_choch_is_irrelevant_to_position():
    values = inputs()
    values["adverse_choch"] = structural_event(
        R2BEvaluationFamily.PROTECTED_CLOSE_CROSS,
        R2BOutcome.CHOCH_CONFIRMED, Side.LONG, "bar-choch",
    )
    result = evaluate_structural_management(**values)
    assert result.action is ManagementAction.HOLD
    assert result.reason is ManagementReason.IRRELEVANT_CHOCH_DIRECTION


def test_explicit_thesis_invalidation_produces_research_exit_signal():
    values = inputs()
    thesis = values["thesis"]
    invalidated = transition_thesis(
        thesis, target=ThesisStatus.INVALIDATED,
        event_at=BASE + timedelta(minutes=19),
        decision_at=BASE + timedelta(minutes=19),
        evidence_ids=("explicit-invalidation",), reason_code="MACRO_BREAK",
        effect_policy=ThesisEffectPolicy.EXPLICIT_INVALIDATION,
        explicit_invalidation_event_id="invalidated-1",
    ).snapshot
    values["thesis"] = invalidated
    result = evaluate_structural_management(**values)
    assert result.action is ManagementAction.EXIT_SIGNAL
    assert result.reason is ManagementReason.THESIS_EXPLICITLY_INVALIDATED
    assert result.execution_authority is False


def test_r5_module_has_no_runtime_or_exchange_surface():
    path = Path(__file__).parents[1] / "strategy_v2" / "r5_management.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = ("exchange", "binance", "main", "telegram", "risk_manager",
                 "requests", "httpx", "aiohttp", "socket", "sqlite3")
    forbidden_calls = ("create_order", "cancel_order", "set_leverage", "connect",
                       "write_text", "write_bytes", "now", "utcnow")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not item.name.lower().startswith(forbidden) for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert not (node.module or "").lower().startswith(forbidden)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls
