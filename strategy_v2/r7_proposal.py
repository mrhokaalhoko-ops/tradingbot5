"""Pure R7 atomic research-proposal assembly with no execution bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .causality import require_utc
from .decisions import StrategyDecision
from .enums import DecisionStatus, Side
from .events import canonical_sha256
from .models import AttemptId, ExitPlanContract, SetupId, ThesisId
from .r3_planning import (
    CausalTargetPlan,
    InitialStopPlan,
    PlanStatus,
    TargetMode,
)
from .r3_thesis import ThesisSnapshot, ThesisStatus
from .r4_portfolio import PortfolioGateResult, PortfolioGateStatus
from .r4_risk import RiskPlan, RiskPlanStatus
from .r6_entry import (
    ClosedTriggerEvidence,
    EntryGateStatus,
    EntryQualification,
    EntryZone,
    ExecutableQuote,
    RegimeContextSnapshot,
)


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _digest(value: str, name: str, length: int = 64) -> None:
    if not isinstance(value, str) or len(value) != length or any(
        character not in "0123456789abcdefABCDEF" for character in value
    ):
        raise ValueError(f"{name} must be a {length}-character hex digest")


class ProposalAssemblyStatus(str, Enum):
    READY = "READY"
    NO_TRADE = "NO_TRADE"
    UNRESOLVED = "UNRESOLVED"


class ProposalAssemblyReason(str, Enum):
    THESIS_NOT_WAITING_TRIGGER = "THESIS_NOT_WAITING_TRIGGER"
    COMPONENT_NOT_READY = "COMPONENT_NOT_READY"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    CONTRACT_EPOCH_MISMATCH = "CONTRACT_EPOCH_MISMATCH"
    GEOMETRY_MISMATCH = "GEOMETRY_MISMATCH"
    PORTFOLIO_RISK_REFERENCE_MISMATCH = "PORTFOLIO_RISK_REFERENCE_MISMATCH"
    COMPONENT_DECISION_TIME_MISMATCH = "COMPONENT_DECISION_TIME_MISMATCH"
    FUTURE_SOURCE_EVIDENCE = "FUTURE_SOURCE_EVIDENCE"
    EXECUTION_AUTHORITY_PRESENT = "EXECUTION_AUTHORITY_PRESENT"


@dataclass(frozen=True, slots=True)
class ProposalAssemblyPolicy:
    source_commit: str
    policy_id: str
    rule_version: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        _text(self.policy_id, "policy_id")
        _text(self.rule_version, "rule_version")
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.policy_id, self.rule_version,
        )).lower())


@dataclass(frozen=True, slots=True)
class AtomicResearchProposal:
    decision: StrategyDecision
    contract_epoch_id: str
    entry_qualification_id: str
    stop_plan_id: str
    target_plan_id: str
    risk_plan_id: str
    portfolio_result_id: str
    policy_variant_hash: str
    assembly_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.decision, StrategyDecision):
            raise TypeError("typed strategy decision required")
        if self.decision.status is not DecisionStatus.PROPOSAL_CANDIDATE:
            raise ValueError("atomic proposal requires candidate decision")
        if (
            type(self.decision.thesis_id) is not ThesisId
            or type(self.decision.setup_id) is not SetupId
            or type(self.decision.attempt_id) is not AttemptId
        ):
            raise ValueError("atomic proposal requires thesis, setup, and attempt identities")
        if self.decision.execution_authority:
            raise ValueError("atomic research proposal cannot carry execution authority")
        for name in (
            "contract_epoch_id", "entry_qualification_id", "stop_plan_id",
            "target_plan_id", "risk_plan_id", "portfolio_result_id",
        ):
            _text(getattr(self, name), name)
        _digest(self.policy_variant_hash, "policy_variant_hash")
        object.__setattr__(self, "assembly_id", "asm_" + canonical_sha256((
            self.decision.symbol, self.decision.side, self.decision.thesis_id,
            self.decision.setup_id, self.decision.attempt_id,
            self.contract_epoch_id, self.entry_qualification_id,
            self.stop_plan_id, self.target_plan_id, self.risk_plan_id,
            self.portfolio_result_id, self.policy_variant_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class ProposalAssemblyResult:
    status: ProposalAssemblyStatus
    reason: ProposalAssemblyReason | None
    proposal: AtomicResearchProposal | None
    decision_at: datetime
    result_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProposalAssemblyStatus):
            raise TypeError("typed assembly status required")
        require_utc(self.decision_at, "decision_at")
        if self.status is ProposalAssemblyStatus.READY:
            if self.reason is not None or not isinstance(
                self.proposal, AtomicResearchProposal
            ):
                raise ValueError("ready assembly needs proposal and no reason")
        elif not isinstance(self.reason, ProposalAssemblyReason) or self.proposal is not None:
            raise ValueError("non-ready assembly needs typed reason and no proposal")
        object.__setattr__(self, "result_id", "asr_" + canonical_sha256((
            self.status, self.reason,
            self.proposal.assembly_id if self.proposal else None,
        )).lower())


def _result(
    status: ProposalAssemblyStatus,
    reason: ProposalAssemblyReason | None,
    decision_at: datetime,
    proposal: AtomicResearchProposal | None = None,
) -> ProposalAssemblyResult:
    return ProposalAssemblyResult(status, reason, proposal, decision_at)


def assemble_research_proposal(
    *,
    thesis: ThesisSnapshot,
    regime: RegimeContextSnapshot,
    zone: EntryZone,
    trigger: ClosedTriggerEvidence,
    quote: ExecutableQuote,
    entry: EntryQualification,
    stop: InitialStopPlan,
    target: CausalTargetPlan,
    risk: RiskPlan,
    portfolio: PortfolioGateResult,
    policy: ProposalAssemblyPolicy,
    decision_at: datetime,
) -> ProposalAssemblyResult:
    """Atomically bind approved research components without creating an order intent."""

    require_utc(decision_at, "decision_at")
    typed_inputs = (
        (thesis, ThesisSnapshot), (regime, RegimeContextSnapshot),
        (zone, EntryZone), (trigger, ClosedTriggerEvidence),
        (quote, ExecutableQuote), (entry, EntryQualification),
        (stop, InitialStopPlan), (target, CausalTargetPlan),
        (risk, RiskPlan), (portfolio, PortfolioGateResult),
        (policy, ProposalAssemblyPolicy),
    )
    if not all(isinstance(value, expected) for value, expected in typed_inputs):
        raise TypeError("typed immutable proposal inputs required")

    authorities = tuple(getattr(value, "execution_authority", True)
                        for value, _ in typed_inputs)
    if any(authorities):
        return _result(
            ProposalAssemblyStatus.UNRESOLVED,
            ProposalAssemblyReason.EXECUTION_AUTHORITY_PRESENT,
            decision_at,
        )
    if thesis.status is not ThesisStatus.WAIT_TRIGGER:
        return _result(
            ProposalAssemblyStatus.NO_TRADE,
            ProposalAssemblyReason.THESIS_NOT_WAITING_TRIGGER,
            decision_at,
        )
    if (
        entry.status is EntryGateStatus.UNRESOLVED
        or stop.status is PlanStatus.UNRESOLVED
        or target.status is PlanStatus.UNRESOLVED
        or risk.status is RiskPlanStatus.UNRESOLVED
        or portfolio.status is PortfolioGateStatus.UNRESOLVED
    ):
        return _result(
            ProposalAssemblyStatus.UNRESOLVED,
            ProposalAssemblyReason.COMPONENT_NOT_READY,
            decision_at,
        )
    if not (
        entry.status is EntryGateStatus.READY
        and stop.status is PlanStatus.READY
        and target.status is PlanStatus.READY
        and risk.status is RiskPlanStatus.READY
        and portfolio.status is PortfolioGateStatus.APPROVED
    ):
        return _result(
            ProposalAssemblyStatus.NO_TRADE,
            ProposalAssemblyReason.COMPONENT_NOT_READY,
            decision_at,
        )

    thesis_id = thesis.identity.thesis_id
    side = thesis.identity.side
    symbol = thesis.identity.symbol
    if not (
        entry.thesis_id == stop.thesis_id == risk.thesis_id == thesis_id
        and entry.side is stop.side is target.side is risk.side is side
        and risk.symbol == regime.symbol == zone.symbol == trigger.symbol
        == quote.symbol == symbol
        and zone.thesis_id == trigger.thesis_id == thesis_id
        and zone.side is trigger.side is side
        and entry.zone_id == zone.zone_id == trigger.zone_id
        and entry.trigger_evidence_id == trigger.evidence_id
        and entry.regime_context_id == regime.context_id
        and entry.quote_id == quote.quote_id
    ):
        return _result(
            ProposalAssemblyStatus.UNRESOLVED,
            ProposalAssemblyReason.IDENTITY_MISMATCH,
            decision_at,
        )

    epoch_id = risk.contract_epoch_id
    if not (
        regime.contract_epoch_id == zone.contract_epoch_id
        == trigger.contract_epoch_id == quote.contract_epoch_id == epoch_id
    ):
        return _result(
            ProposalAssemblyStatus.UNRESOLVED,
            ProposalAssemblyReason.CONTRACT_EPOCH_MISMATCH,
            decision_at,
        )

    if not (
        entry.executable_price == stop.entry_price == target.entry_price
        == risk.entry_price
        and stop.final_stop == target.stop_price == risk.hard_stop_price
    ):
        return _result(
            ProposalAssemblyStatus.UNRESOLVED,
            ProposalAssemblyReason.GEOMETRY_MISMATCH,
            decision_at,
        )
    if portfolio.candidate_risk_plan_id != risk.plan_id:
        return _result(
            ProposalAssemblyStatus.UNRESOLVED,
            ProposalAssemblyReason.PORTFOLIO_RISK_REFERENCE_MISMATCH,
            decision_at,
        )

    component_times = (
        entry.decision_at, stop.decision_at, target.decision_at,
        risk.decision_at, portfolio.decision_at,
    )
    if any(component_time != decision_at for component_time in component_times):
        return _result(
            ProposalAssemblyStatus.UNRESOLVED,
            ProposalAssemblyReason.COMPONENT_DECISION_TIME_MISMATCH,
            decision_at,
        )
    source_times = (
        thesis.events[-1].decision_at, regime.available_at, zone.available_at,
        trigger.available_at, quote.available_at,
        target.reward_reference.times.available_at,
        target.selected_target.times.available_at if target.selected_target else None,
    )
    if any(source_time is not None and source_time > decision_at
           for source_time in source_times):
        return _result(
            ProposalAssemblyStatus.UNRESOLVED,
            ProposalAssemblyReason.FUTURE_SOURCE_EVIDENCE,
            decision_at,
        )

    setup_id = SetupId("set_" + canonical_sha256((
        thesis_id, zone.zone_id, entry.policy_variant_hash,
        stop.policy_variant_hash, target.policy_variant_hash,
        risk.policy_variant_hash, portfolio.policy_variant_hash,
        policy.variant_hash,
    )).lower())
    attempt_id = AttemptId("att_" + canonical_sha256((
        setup_id, trigger.evidence_id, quote.quote_id,
        entry.qualification_id, risk.filter_id,
    )).lower())

    exit_plan = ExitPlanContract(
        policy_id=target.policy_variant_hash,
        reward_reference=target.reward_reference,
        exit_target=target.selected_target,
        final_tp_required=target.mode is TargetMode.STRUCTURAL_FINAL_TP,
        exit_event_types=target.runner_exit_event_types,
    )
    evidence_ids = tuple(dict.fromkeys((
        thesis.snapshot_id, regime.context_id, zone.zone_hash,
        trigger.evidence_id, quote.quote_id, entry.qualification_id,
        stop.plan_id, target.plan_id, risk.plan_id, portfolio.result_id,
    )))
    decision = StrategyDecision(
        status=DecisionStatus.PROPOSAL_CANDIDATE,
        symbol=symbol,
        side=side,
        decision_at=decision_at,
        stage="R7_ATOMIC_RESEARCH_PROPOSAL",
        failed_invariant=None,
        input_evidence_ids=evidence_ids,
        thesis_id=thesis_id,
        setup_id=setup_id,
        attempt_id=attempt_id,
        exit_plan=exit_plan,
    )
    proposal = AtomicResearchProposal(
        decision=decision,
        contract_epoch_id=epoch_id,
        entry_qualification_id=entry.qualification_id,
        stop_plan_id=stop.plan_id,
        target_plan_id=target.plan_id,
        risk_plan_id=risk.plan_id,
        portfolio_result_id=portfolio.result_id,
        policy_variant_hash=policy.variant_hash,
    )
    return _result(ProposalAssemblyStatus.READY, None, decision_at, proposal)
