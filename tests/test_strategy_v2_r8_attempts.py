"""R8 append-only attempt admission and re-entry policy tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_v2.causality import CausalTimes
from strategy_v2.decisions import StrategyDecision
from strategy_v2.enums import DecisionStatus, Side
from strategy_v2.events import canonical_sha256
from strategy_v2.models import (
    AttemptId,
    ExitPlanContract,
    ExitTarget,
    RewardReference,
    SetupId,
    ThesisId,
)
from strategy_v2.r3_thesis import ResetEvidence, ReentryClassification
from strategy_v2.r7_proposal import AtomicResearchProposal
from strategy_v2.r8_attempts import (
    AttemptAdmissionReason,
    AttemptAdmissionStatus,
    AttemptEventKind,
    AttemptLedgerEvent,
    admit_attempt,
    record_attempt_close,
)


D = Decimal
BASE = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
H = lambda value: canonical_sha256((value,)).lower()


def proposal(
    *,
    thesis_key="one",
    setup_key="one",
    attempt_key="one",
    decision_at=BASE,
):
    thesis_id = ThesisId(f"ths_{thesis_key}")
    setup_id = SetupId(f"set_{setup_key}")
    attempt_id = AttemptId(f"att_{attempt_key}")
    times = CausalTimes(
        decision_at - timedelta(minutes=3),
        decision_at - timedelta(minutes=2),
        decision_at - timedelta(minutes=1),
    )
    reward = RewardReference("reward-1", D("108"), Side.LONG, "level-1", times)
    target = ExitTarget("target-1", D("108"), Side.LONG, "level-1", times)
    decision = StrategyDecision(
        DecisionStatus.PROPOSAL_CANDIDATE,
        "ABCUSDT",
        Side.LONG,
        decision_at,
        "R7_ATOMIC_RESEARCH_PROPOSAL",
        None,
        ("thesis", "trigger", "quote", "risk", "portfolio"),
        thesis_id=thesis_id,
        setup_id=setup_id,
        attempt_id=attempt_id,
        exit_plan=ExitPlanContract("target-policy", reward, target, True, ()),
    )
    return AtomicResearchProposal(
        decision,
        "ABC-PERP-E1",
        "entry-qualification",
        "stop-plan",
        "target-plan",
        "risk-plan",
        "portfolio-result",
        H("assembly-policy"),
    )


def reset_for(thesis_id, *, close_at, available=None, context_valid=True):
    confirmed = close_at + timedelta(minutes=1)
    available = available or confirmed + timedelta(seconds=1)
    return ResetEvidence(
        "reset-event", thesis_id, confirmed, available,
        ("post-close-sweep", "new-trigger"), context_valid,
    )


def admit_new(item, history=()):
    return admit_attempt(
        proposal=item,
        history=history,
        reset=None,
        decision_at=item.decision.decision_at,
    )


def close(item, admission, history, *, minutes=1):
    closed_at = item.decision.decision_at + timedelta(minutes=minutes)
    event = record_attempt_close(
        admission=admission,
        history=history,
        closed_at=closed_at,
        available_at=closed_at + timedelta(seconds=1),
        source_event_ids=("position-flat", "close-confirmed"),
    )
    return event


def test_new_thesis_is_admitted_without_execution_authority():
    item = proposal()
    result = admit_new(item)
    assert result.status is AttemptAdmissionStatus.ADMITTED
    assert result.classification is ReentryClassification.NEW_THESIS
    assert result.admission_event.kind is AttemptEventKind.ADMITTED
    assert result.admission_event.attempt_id == item.decision.attempt_id
    assert result.execution_authority is False
    assert result.admission_event.execution_authority is False


def test_identical_attempt_is_blocked_across_scanner_retries():
    item = proposal()
    first = admit_new(item).admission_event
    result = admit_new(item, (first,))
    assert result.status is AttemptAdmissionStatus.BLOCKED
    assert result.reason is AttemptAdmissionReason.DUPLICATE_ATTEMPT
    assert result.admission_event is None


def test_same_thesis_new_attempt_is_blocked_while_prior_is_open():
    first_item = proposal()
    first = admit_new(first_item).admission_event
    second_item = proposal(attempt_key="two")
    result = admit_new(second_item, (first,))
    assert result.status is AttemptAdmissionStatus.BLOCKED
    assert result.reason is AttemptAdmissionReason.PRIOR_ATTEMPT_OPEN


def test_same_thesis_after_close_requires_causal_reset():
    first_item = proposal()
    admission = admit_new(first_item).admission_event
    close_event = close(first_item, admission, (admission,))
    second_item = proposal(
        attempt_key="two", decision_at=close_event.available_at + timedelta(minutes=2),
    )
    result = admit_new(second_item, (admission, close_event))
    assert result.status is AttemptAdmissionStatus.BLOCKED
    assert result.reason is AttemptAdmissionReason.SAME_THESIS_RESET_MISSING


def test_same_thesis_valid_post_close_reset_is_admitted():
    first_item = proposal()
    admission = admit_new(first_item).admission_event
    close_event = close(first_item, admission, (admission,))
    second_item = proposal(
        setup_key="two", attempt_key="two",
        decision_at=close_event.available_at + timedelta(minutes=3),
    )
    reset = reset_for(
        first_item.decision.thesis_id, close_at=close_event.event_at,
    )
    result = admit_attempt(
        proposal=second_item,
        history=(admission, close_event),
        reset=reset,
        decision_at=second_item.decision.decision_at,
    )
    assert result.status is AttemptAdmissionStatus.ADMITTED
    assert result.classification is ReentryClassification.SAME_THESIS_VALID_RESET
    assert close_event.event_id in result.admission_event.source_event_ids
    assert reset.reset_id in result.admission_event.source_event_ids


def test_invalid_or_future_reset_fails_closed():
    first_item = proposal()
    admission = admit_new(first_item).admission_event
    close_event = close(first_item, admission, (admission,))
    second_item = proposal(
        attempt_key="two", decision_at=close_event.available_at + timedelta(minutes=3),
    )
    invalid = reset_for(
        first_item.decision.thesis_id,
        close_at=close_event.event_at,
        context_valid=False,
    )
    result = admit_attempt(
        proposal=second_item, history=(admission, close_event), reset=invalid,
        decision_at=second_item.decision.decision_at,
    )
    assert result.reason is AttemptAdmissionReason.CONTEXT_INVALIDATED
    future = reset_for(
        first_item.decision.thesis_id,
        close_at=close_event.event_at,
        available=second_item.decision.decision_at + timedelta(seconds=1),
    )
    result = admit_attempt(
        proposal=second_item, history=(admission, close_event), reset=future,
        decision_at=second_item.decision.decision_at,
    )
    assert result.status is AttemptAdmissionStatus.UNRESOLVED
    assert result.reason is AttemptAdmissionReason.RESET_EVIDENCE_FUTURE


def test_new_thesis_identity_does_not_recycle_prior_thesis():
    first_item = proposal()
    first = admit_new(first_item).admission_event
    second_item = proposal(
        thesis_key="two", setup_key="two", attempt_key="two",
    )
    result = admit_new(second_item, (first,))
    assert result.status is AttemptAdmissionStatus.ADMITTED
    assert result.classification is ReentryClassification.NEW_THESIS


def test_close_is_append_only_and_cannot_be_duplicated():
    item = proposal()
    admission = admit_new(item).admission_event
    close_event = close(item, admission, (admission,))
    assert close_event.kind is AttemptEventKind.CLOSED
    with pytest.raises(ValueError, match="one open admission"):
        close(item, admission, (admission, close_event), minutes=2)
    with pytest.raises(ValueError, match="cannot predate admission"):
        record_attempt_close(
            admission=admission,
            history=(admission,),
            closed_at=admission.event_at - timedelta(seconds=1),
            available_at=admission.available_at,
            source_event_ids=("invalid-close",),
        )


def test_invalid_or_out_of_order_history_is_unresolved():
    item = proposal()
    admission = admit_new(item).admission_event
    duplicate = replace(
        admission,
        event_at=admission.event_at + timedelta(seconds=1),
        available_at=admission.available_at + timedelta(seconds=1),
    )
    candidate = proposal(attempt_key="two")
    result = admit_new(candidate, (admission, duplicate))
    assert result.status is AttemptAdmissionStatus.UNRESOLVED
    assert result.reason is AttemptAdmissionReason.HISTORY_INVALID


def test_any_older_open_attempt_blocks_same_thesis_even_if_newer_is_closed():
    first_item = proposal(attempt_key="one")
    first = admit_new(first_item).admission_event
    second_item = proposal(
        attempt_key="two", decision_at=BASE + timedelta(minutes=1),
    )
    second = AttemptLedgerEvent(
        AttemptEventKind.ADMITTED,
        second_item.decision.thesis_id,
        second_item.decision.setup_id,
        second_item.decision.attempt_id,
        second_item.assembly_id,
        second_item.decision.decision_at,
        second_item.decision.decision_at,
        second_item.decision.input_evidence_ids,
    )
    second_close = record_attempt_close(
        admission=second,
        history=(first, second),
        closed_at=BASE + timedelta(minutes=2),
        available_at=BASE + timedelta(minutes=2, seconds=1),
        source_event_ids=("second-close",),
    )
    third_item = proposal(
        attempt_key="three", decision_at=BASE + timedelta(minutes=4),
    )
    valid_reset = reset_for(
        third_item.decision.thesis_id,
        close_at=second_close.event_at,
    )
    result = admit_attempt(
        proposal=third_item,
        history=(first, second, second_close),
        reset=valid_reset,
        decision_at=third_item.decision.decision_at,
    )
    assert result.status is AttemptAdmissionStatus.BLOCKED
    assert result.reason is AttemptAdmissionReason.PRIOR_ATTEMPT_OPEN


def test_decision_time_is_atomic_with_proposal():
    item = proposal()
    result = admit_attempt(
        proposal=item,
        history=(),
        reset=None,
        decision_at=item.decision.decision_at + timedelta(seconds=1),
    )
    assert result.status is AttemptAdmissionStatus.UNRESOLVED
    assert result.reason is AttemptAdmissionReason.DECISION_TIME_MISMATCH


def test_admission_does_not_mutate_supplied_history():
    first_item = proposal()
    admission = admit_new(first_item).admission_event
    history = (admission,)
    before = tuple(event.event_id for event in history)
    admit_new(proposal(thesis_key="two", setup_key="two", attempt_key="two"), history)
    assert tuple(event.event_id for event in history) == before


def test_r8_has_no_exchange_order_or_runtime_imports():
    path = Path(__file__).parents[1] / "strategy_v2" / "r8_attempts.py"
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
