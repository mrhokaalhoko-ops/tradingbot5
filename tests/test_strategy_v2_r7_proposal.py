"""R7 atomic research-proposal assembly tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_v2.causality import CausalTimes
from strategy_v2.enums import DecisionStatus, Side
from strategy_v2.events import canonical_sha256
from strategy_v2.models import ExitTarget, RewardReference, ThesisId
from strategy_v2.r3_planning import (
    CausalTargetPlan,
    InitialStopPlan,
    PlanStatus,
    TargetMode,
)
from strategy_v2.r3_thesis import (
    ThesisEffectPolicy,
    ThesisIdentity,
    ThesisSnapshot,
    ThesisStatus,
    ThesisTransitionEvent,
)
from strategy_v2.r4_portfolio import PortfolioGateResult, PortfolioGateStatus
from strategy_v2.r4_risk import RiskPlan, RiskPlanStatus
from strategy_v2.r6_entry import (
    ClosedTriggerEvidence,
    EntryGateReason,
    EntryGateStatus,
    EntryQualification,
    EntryZone,
    ExecutableQuote,
    RegimeContextSnapshot,
    RegimeLabel,
    TriggerType,
)
from strategy_v2.r7_proposal import (
    ProposalAssemblyPolicy,
    ProposalAssemblyReason,
    ProposalAssemblyStatus,
    assemble_research_proposal,
)
from strategy_v2.r9_replay import build_replay_case


D = Decimal
BASE = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
DECISION = BASE + timedelta(minutes=10)
COMMIT = "a1e37e6bfe05d66ffe5d3bdb4a916a41feb3bf9b"
H = lambda value: canonical_sha256((value,)).lower()


def thesis(side=Side.LONG, status=ThesisStatus.WAIT_TRIGGER):
    thesis_id = ThesisId("ths_" + H((side, "thesis")))
    identity = ThesisIdentity(
        thesis_id, "ABCUSDT", side, "BOS_RETEST", "epoch-1", "break-1",
        H("htf"), ("zone-source",), "invalidation-1", H("thesis-policy"),
    )
    transitions = [
        ThesisTransitionEvent(
            thesis_id, None, ThesisStatus.OBSERVED, BASE, BASE,
            ("observed",), "OBSERVED", ThesisEffectPolicy.NONE,
        ),
        ThesisTransitionEvent(
            thesis_id, ThesisStatus.OBSERVED, ThesisStatus.QUALIFIED,
            BASE + timedelta(minutes=1), BASE + timedelta(minutes=1),
            ("qualified",), "QUALIFIED", ThesisEffectPolicy.NONE,
        ),
    ]
    if status is ThesisStatus.WAIT_TRIGGER:
        transitions.append(ThesisTransitionEvent(
            thesis_id, ThesisStatus.QUALIFIED, ThesisStatus.WAIT_TRIGGER,
            BASE + timedelta(minutes=2), BASE + timedelta(minutes=2),
            ("wait-trigger",), "WAIT_TRIGGER", ThesisEffectPolicy.NONE,
        ))
    return ThesisSnapshot(
        identity, status, transitions[0].event_at, transitions[-1].event_at,
        tuple(transitions),
    )


def bundle(side=Side.LONG, runner=False):
    current_thesis = thesis(side)
    thesis_id = current_thesis.identity.thesis_id
    epoch = "ABC-PERP-E1"
    regime = RegimeContextSnapshot(
        "ABCUSDT", epoch,
        RegimeLabel.TREND_UP if side is Side.LONG else RegimeLabel.TREND_DOWN,
        ("4h", "1h"), BASE + timedelta(minutes=3),
        BASE + timedelta(minutes=4), ("regime-4h", "regime-1h"), H("regime"),
    )
    zone = EntryZone(
        "zone-1", thesis_id, "ABCUSDT", epoch, side, "15m",
        D("99"), D("101"), BASE + timedelta(minutes=4),
        BASE + timedelta(minutes=5), BASE + timedelta(hours=1),
        ("zone-source", "zone-confirmation"),
    )
    trigger = ClosedTriggerEvidence(
        "trigger-1", thesis_id, zone.zone_id, "ABCUSDT", epoch, side, "5m",
        TriggerType.CLOSED_RETEST_REJECTION, "5m-bar-1", D("100"),
        BASE + timedelta(minutes=8), BASE + timedelta(minutes=9), True,
        H("trigger-source"),
    )
    quote = ExecutableQuote(
        "ABCUSDT", epoch, D("100.0"), D("100.1"),
        BASE + timedelta(minutes=9, seconds=30),
        BASE + timedelta(minutes=9, seconds=31), H("quote-source"),
    )
    entry_price = D("100.1") if side is Side.LONG else D("100.0")
    stop_price = D("98") if side is Side.LONG else D("102")
    invalidation_price = D("98.5") if side is Side.LONG else D("101.5")
    target_price = D("108") if side is Side.LONG else D("92")
    entry = EntryQualification(
        EntryGateStatus.READY, None, thesis_id, side, zone.zone_id,
        trigger.evidence_id, regime.context_id, quote.quote_id,
        entry_price, D("0"), D("0.5"), D("0.1"), D("0.001"),
        H("entry-policy"), DECISION,
    )
    stop = InitialStopPlan(
        PlanStatus.READY, None, thesis_id, side, entry_price, (),
        "invalidation-1", invalidation_price, D("0.5"), stop_price,
        abs(entry_price - stop_price), H("stop-policy"), DECISION,
    )
    times = CausalTimes(
        BASE + timedelta(minutes=3), BASE + timedelta(minutes=4),
        BASE + timedelta(minutes=5),
    )
    reward = RewardReference("reward-1", target_price, side, "liquidity-1", times)
    selected = None if runner else ExitTarget(
        "target-1", target_price, side, "liquidity-1", times,
    )
    target = CausalTargetPlan(
        PlanStatus.READY, None,
        TargetMode.FULL_POSITION_RUNNER if runner else TargetMode.STRUCTURAL_FINAL_TP,
        side, entry_price, stop_price, (), selected, reward,
        None if runner else D("3.76190476"),
        ("STRUCTURE_TRAIL_EXIT", "THESIS_INVALIDATED") if runner else (),
        H("target-policy"), DECISION,
    )
    risk = RiskPlan(
        RiskPlanStatus.READY, None, thesis_id, side, "ABCUSDT", epoch,
        entry_price, stop_price, D("10000"), D("10"), D("1001"),
        D("50"), D("100"), D("2.1"), D("25"), D("100.1"),
        D("0.5"), "filter-1", H("costs"), H("risk-policy"), DECISION,
    )
    portfolio = PortfolioGateResult(
        PortfolioGateStatus.APPROVED, None, risk.plan_id, 1, D("1001"),
        D("1001" if side is Side.LONG else "-1001"), D("25"), D("25"),
        D("900" if side is Side.LONG else "-900"), H("dependency"),
        H("portfolio-policy"), DECISION,
    )
    return dict(
        thesis=current_thesis, regime=regime, zone=zone, trigger=trigger,
        quote=quote, entry=entry, stop=stop, target=target, risk=risk,
        portfolio=portfolio,
        policy=ProposalAssemblyPolicy(COMMIT, "ATOMIC_PROPOSAL_V1", "r7"),
        decision_at=DECISION,
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_atomic_proposal_binds_all_ready_components(side):
    values = bundle(side)
    result = assemble_research_proposal(**values)
    assert result.status is ProposalAssemblyStatus.READY
    assert result.reason is None
    assert result.execution_authority is False
    proposal = result.proposal
    assert proposal.execution_authority is False
    assert proposal.decision.status is DecisionStatus.PROPOSAL_CANDIDATE
    assert proposal.decision.execution_authority is False
    assert proposal.decision.thesis_id == values["thesis"].identity.thesis_id
    assert proposal.decision.exit_plan.exit_target.price == D("108" if side is Side.LONG else "92")
    assert proposal.risk_plan_id == values["risk"].plan_id
    assert proposal.portfolio_result_id == values["portfolio"].result_id


def test_runner_preserves_event_exit_and_does_not_fabricate_target():
    result = assemble_research_proposal(**bundle(runner=True))
    assert result.status is ProposalAssemblyStatus.READY
    exit_plan = result.proposal.decision.exit_plan
    assert exit_plan.final_tp_required is False
    assert exit_plan.exit_target is None
    assert exit_plan.exit_event_types == ("STRUCTURE_TRAIL_EXIT", "THESIS_INVALIDATED")


def test_structural_target_is_preserved_not_replaced_by_fixed_four_r():
    result = assemble_research_proposal(**bundle())
    assert result.proposal.decision.exit_plan.exit_target.price == D("108")
    assert result.proposal.decision.exit_plan.reward_reference.price == D("108")
    assert result.proposal.decision.exit_plan.exit_target.price != D("108.5")


def test_not_waiting_thesis_and_nonready_component_fail_closed():
    values = bundle()
    values["thesis"] = thesis(status=ThesisStatus.QUALIFIED)
    result = assemble_research_proposal(**values)
    assert result.status is ProposalAssemblyStatus.NO_TRADE
    assert result.reason is ProposalAssemblyReason.THESIS_NOT_WAITING_TRIGGER
    values = bundle()
    values["entry"] = replace(
        values["entry"], status=EntryGateStatus.NO_TRADE,
        reason=EntryGateReason.SPREAD_TOO_WIDE,
    )
    result = assemble_research_proposal(**values)
    assert result.reason is ProposalAssemblyReason.COMPONENT_NOT_READY
    assert result.proposal is None
    values = bundle()
    values["entry"] = replace(
        values["entry"], status=EntryGateStatus.UNRESOLVED,
        reason=EntryGateReason.QUOTE_STALE,
    )
    result = assemble_research_proposal(**values)
    assert result.status is ProposalAssemblyStatus.UNRESOLVED
    assert result.reason is ProposalAssemblyReason.COMPONENT_NOT_READY


def test_cross_component_identity_and_geometry_mismatches_are_rejected():
    values = bundle()
    values["risk"] = replace(values["risk"], thesis_id=ThesisId("ths_other"))
    result = assemble_research_proposal(**values)
    assert result.reason is ProposalAssemblyReason.IDENTITY_MISMATCH
    values = bundle()
    values["risk"] = replace(values["risk"], hard_stop_price=D("97.9"))
    result = assemble_research_proposal(**values)
    assert result.reason is ProposalAssemblyReason.GEOMETRY_MISMATCH


def test_contract_epoch_and_portfolio_reference_are_bound():
    values = bundle()
    changed_quote = replace(values["quote"], contract_epoch_id="ABC-PERP-E2")
    values["quote"] = changed_quote
    values["entry"] = replace(values["entry"], quote_id=changed_quote.quote_id)
    result = assemble_research_proposal(**values)
    assert result.reason is ProposalAssemblyReason.CONTRACT_EPOCH_MISMATCH
    values = bundle()
    values["portfolio"] = replace(
        values["portfolio"], candidate_risk_plan_id="rsk_other",
    )
    result = assemble_research_proposal(**values)
    assert result.reason is ProposalAssemblyReason.PORTFOLIO_RISK_REFERENCE_MISMATCH


def test_atomic_decision_time_and_source_causality_are_enforced():
    values = bundle()
    values["stop"] = replace(
        values["stop"], decision_at=DECISION - timedelta(seconds=1),
    )
    result = assemble_research_proposal(**values)
    assert result.reason is ProposalAssemblyReason.COMPONENT_DECISION_TIME_MISMATCH
    values = bundle()
    changed_quote = replace(
        values["quote"], available_at=DECISION + timedelta(seconds=1),
    )
    values["quote"] = changed_quote
    values["entry"] = replace(values["entry"], quote_id=changed_quote.quote_id)
    result = assemble_research_proposal(**values)
    assert result.reason is ProposalAssemblyReason.FUTURE_SOURCE_EVIDENCE


def test_setup_and_attempt_identity_are_polling_stable_but_quote_sensitive():
    first_values = bundle()
    first = assemble_research_proposal(**first_values).proposal
    later_values = bundle()
    later_decision = DECISION + timedelta(seconds=1)
    for key in ("entry", "stop", "target", "risk", "portfolio"):
        later_values[key] = replace(later_values[key], decision_at=later_decision)
    later_values["decision_at"] = later_decision
    later = assemble_research_proposal(**later_values).proposal
    assert first.decision.setup_id == later.decision.setup_id
    assert first.decision.attempt_id == later.decision.attempt_id
    assert first.assembly_id == later.assembly_id

    changed_values = bundle()
    changed_quote = replace(
        changed_values["quote"], bid=D("99.9"), ask=D("100.0"),
        source_hash=H("new-quote"),
    )
    changed_values["quote"] = changed_quote
    changed_values["entry"] = replace(
        changed_values["entry"], quote_id=changed_quote.quote_id,
        executable_price=D("100.0"),
    )
    changed_values["stop"] = replace(changed_values["stop"], entry_price=D("100.0"))
    changed_values["target"] = replace(changed_values["target"], entry_price=D("100.0"))
    changed_values["risk"] = replace(changed_values["risk"], entry_price=D("100.0"))
    changed_values["portfolio"] = replace(
        changed_values["portfolio"],
        candidate_risk_plan_id=changed_values["risk"].plan_id,
    )
    changed = assemble_research_proposal(**changed_values).proposal
    assert first.decision.setup_id == changed.decision.setup_id
    assert first.decision.attempt_id != changed.decision.attempt_id


def test_evidence_is_unique_and_complete():
    values = bundle()
    proposal = assemble_research_proposal(**values).proposal
    evidence = proposal.decision.input_evidence_ids
    assert len(evidence) == len(set(evidence)) == 10
    assert values["entry"].qualification_id in evidence
    assert values["portfolio"].result_id in evidence


def test_atomic_contract_requires_all_research_identities():
    proposal = assemble_research_proposal(**bundle()).proposal
    malformed_decision = replace(proposal.decision, attempt_id=None)
    with pytest.raises(ValueError, match="thesis, setup, and attempt"):
        replace(proposal, decision=malformed_decision)


def test_r7_components_build_one_consistent_r9_replay_case():
    values = bundle()
    proposal = assemble_research_proposal(**values).proposal
    case = build_replay_case(
        proposal=proposal,
        entry=values["entry"],
        stop=values["stop"],
        target=values["target"],
        risk=values["risk"],
    )
    assert case.proposal_assembly_id == proposal.assembly_id
    assert case.entry_price == values["entry"].executable_price
    assert case.stop_price == values["stop"].final_stop
    assert case.target_price == values["target"].selected_target.price
    assert case.execution_authority is False


def test_r7_has_no_execution_or_exchange_surface():
    path = Path(__file__).parents[1] / "strategy_v2" / "r7_proposal.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = ("binance", "exchange", "order", "main", "state_manager")
    assert not any(any(term in name.lower() for term in forbidden) for name in imports)
    assert "ProposalId" not in source
    assert "OrderIntent" not in source
    assert "execution_authority: bool = field(default=False, init=False)" in source
