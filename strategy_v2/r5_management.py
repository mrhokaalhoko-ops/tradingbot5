"""Pure structural position-management decisions with no exchange authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .causality import require_utc
from .enums import InvalidationRelation, InvalidationScope, Side
from .events import canonical_sha256
from .models import InvalidationLevel, ThesisId
from .r2b_models import R2BEvaluationFamily, R2BOutcome, R2BStructuralEvaluation
from .r3_planning import PlanStatus, StopPolicySpec, build_initial_stop_plan
from .r3_thesis import ThesisSnapshot, ThesisStatus


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _digest(value: str, name: str, length: int = 64) -> None:
    if not isinstance(value, str) or len(value) != length or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ValueError(f"{name} must be a {length}-character hex digest")


def _price(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be positive finite Decimal")


class ManagementAction(str, Enum):
    HOLD = "HOLD"
    RATCHET_CANDIDATE = "RATCHET_CANDIDATE"
    EXIT_SIGNAL = "EXIT_SIGNAL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNRESOLVED = "UNRESOLVED"


class ManagementReason(str, Enum):
    NO_NEW_PROTECTED_STRUCTURE = "NO_NEW_PROTECTED_STRUCTURE"
    POSITION_EVIDENCE_FUTURE = "POSITION_EVIDENCE_FUTURE"
    THESIS_EVIDENCE_FUTURE = "THESIS_EVIDENCE_FUTURE"
    STRUCTURE_EVIDENCE_FUTURE = "STRUCTURE_EVIDENCE_FUTURE"
    STRUCTURE_IDENTITY_MISMATCH = "STRUCTURE_IDENTITY_MISMATCH"
    CONTINUATION_NOT_CONFIRMED = "CONTINUATION_NOT_CONFIRMED"
    MARKET_OBSERVATION_FUTURE = "MARKET_OBSERVATION_FUTURE"
    STOP_PLAN_UNRESOLVED = "STOP_PLAN_UNRESOLVED"
    STOP_NOT_IMPROVED = "STOP_NOT_IMPROVED"
    CHOCH_NO_AUTOMATIC_THESIS_EFFECT = "CHOCH_NO_AUTOMATIC_THESIS_EFFECT"
    IRRELEVANT_CHOCH_DIRECTION = "IRRELEVANT_CHOCH_DIRECTION"
    THESIS_EXPLICITLY_INVALIDATED = "THESIS_EXPLICITLY_INVALIDATED"
    CURRENT_PRICE_BEYOND_EFFECTIVE_STOP = "CURRENT_PRICE_BEYOND_EFFECTIVE_STOP"


@dataclass(frozen=True, slots=True)
class ManagementPolicySpec:
    source_commit: str
    policy_id: str
    rule_version: str
    structure_policy_id: str
    choch_effect_policy_id: str
    numeric_policy_id: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        for name in (
            "policy_id", "rule_version", "structure_policy_id",
            "choch_effect_policy_id", "numeric_policy_id",
        ):
            _text(getattr(self, name), name)
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.policy_id, self.rule_version,
            self.structure_policy_id, self.choch_effect_policy_id,
            self.numeric_policy_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class PositionProtectionState:
    position_id: str
    symbol: str
    contract_epoch_id: str
    thesis_id: ThesisId
    side: Side
    entry_price: Decimal
    original_stop: Decimal
    current_effective_stop: Decimal
    stop_sequence: int
    last_confirmed_at: datetime
    source_event_ids: tuple[str, ...]
    state_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("position_id", "symbol", "contract_epoch_id"):
            _text(getattr(self, name), name)
        if type(self.thesis_id) is not ThesisId or not isinstance(self.side, Side):
            raise TypeError("typed thesis and side required")
        for name in ("entry_price", "original_stop", "current_effective_stop"):
            _price(getattr(self, name), name)
        if type(self.stop_sequence) is not int or self.stop_sequence < 0:
            raise ValueError("nonnegative stop sequence required")
        require_utc(self.last_confirmed_at, "last_confirmed_at")
        if (not isinstance(self.source_event_ids, tuple) or not self.source_event_ids
                or len(set(self.source_event_ids)) != len(self.source_event_ids)
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.source_event_ids)):
            raise ValueError("unique position source events required")
        if self.side is Side.LONG:
            if not self.original_stop < self.entry_price or self.current_effective_stop < self.original_stop:
                raise ValueError("LONG protection geometry or monotonicity invalid")
        elif not self.original_stop > self.entry_price or self.current_effective_stop > self.original_stop:
            raise ValueError("SHORT protection geometry or monotonicity invalid")
        object.__setattr__(self, "state_id", "pst_" + canonical_sha256((
            self.position_id, self.symbol, self.contract_epoch_id,
            self.thesis_id, self.side, self.entry_price, self.original_stop,
            self.current_effective_stop, self.stop_sequence,
            self.last_confirmed_at, self.source_event_ids,
        )).lower())


@dataclass(frozen=True, slots=True)
class MarketPriceObservation:
    symbol: str
    contract_epoch_id: str
    price_basis_id: str
    price: Decimal
    observed_at: datetime
    available_at: datetime
    source_hash: str
    observation_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("symbol", "contract_epoch_id", "price_basis_id"):
            _text(getattr(self, name), name)
        _price(self.price, "price")
        require_utc(self.observed_at, "observed_at")
        require_utc(self.available_at, "available_at")
        if self.observed_at > self.available_at:
            raise ValueError("market price cannot be available before observation")
        _digest(self.source_hash, "source_hash")
        object.__setattr__(self, "observation_id", "pxo_" + canonical_sha256((
            self.symbol, self.contract_epoch_id, self.price_basis_id,
            self.price, self.observed_at, self.available_at, self.source_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class ProtectedStructureEvidence:
    level: InvalidationLevel
    continuation: R2BStructuralEvaluation
    evidence_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.level, InvalidationLevel):
            raise TypeError("typed protected level required")
        if (not isinstance(self.continuation, R2BStructuralEvaluation)
                or self.continuation.outcome is not R2BOutcome.CONTINUATION_BOS_CONFIRMED
                or self.continuation.market_event_id is None
                or self.continuation.slot is None
                or self.continuation.slot.evaluation_family is not R2BEvaluationFamily.CONTINUATION_BOS):
            raise ValueError("confirmed continuation BOS required")
        object.__setattr__(self, "evidence_id", "pse_" + canonical_sha256((
            self.level, self.continuation.market_event_id,
            self.continuation.source_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class ManagementDecision:
    action: ManagementAction
    reason: ManagementReason | None
    position_state_id: str
    thesis_snapshot_id: str
    proposed_stop: Decimal | None
    buffer: Decimal | None
    protected_structure_evidence_id: str | None
    source_market_event_id: str | None
    policy_variant_hash: str
    decision_at: datetime
    decision_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.action, ManagementAction):
            raise TypeError("typed management action required")
        for name in ("position_state_id", "thesis_snapshot_id"):
            _text(getattr(self, name), name)
        _digest(self.policy_variant_hash, "policy_variant_hash")
        require_utc(self.decision_at, "decision_at")
        if self.action is ManagementAction.RATCHET_CANDIDATE:
            if self.reason is not None or self.proposed_stop is None or self.buffer is None or self.protected_structure_evidence_id is None:
                raise ValueError("ratchet candidate needs complete structural evidence")
        elif not isinstance(self.reason, ManagementReason):
            raise ValueError("non-ratchet management decision requires typed reason")
        object.__setattr__(self, "decision_id", "mgd_" + canonical_sha256((
            self.action, self.reason, self.position_state_id,
            self.thesis_snapshot_id, self.proposed_stop, self.buffer,
            self.protected_structure_evidence_id,
            self.source_market_event_id, self.policy_variant_hash,
        )).lower())


def _decision(
    action: ManagementAction,
    reason: ManagementReason | None,
    *,
    position: PositionProtectionState,
    thesis: ThesisSnapshot,
    policy: ManagementPolicySpec,
    decision_at: datetime,
    proposed_stop: Decimal | None = None,
    buffer: Decimal | None = None,
    protection: ProtectedStructureEvidence | None = None,
    market_event_id: str | None = None,
) -> ManagementDecision:
    return ManagementDecision(
        action, reason, position.state_id, thesis.snapshot_id,
        proposed_stop, buffer, protection.evidence_id if protection else None,
        market_event_id, policy.variant_hash, decision_at,
    )


def evaluate_structural_management(
    *,
    position: PositionProtectionState,
    thesis: ThesisSnapshot,
    market: MarketPriceObservation,
    protection: ProtectedStructureEvidence | None,
    adverse_choch: R2BStructuralEvaluation | None,
    atr: Decimal,
    tick_size: Decimal,
    spread: Decimal,
    stop_policy: StopPolicySpec,
    policy: ManagementPolicySpec,
    decision_at: datetime,
) -> ManagementDecision:
    """Return research intent only; a ratchet candidate is not an order replacement."""

    require_utc(decision_at, "decision_at")
    if not isinstance(position, PositionProtectionState) or not isinstance(thesis, ThesisSnapshot):
        raise TypeError("typed position and thesis required")
    if not isinstance(market, MarketPriceObservation) or not isinstance(stop_policy, StopPolicySpec) or not isinstance(policy, ManagementPolicySpec):
        raise TypeError("typed market and policies required")
    if (position.thesis_id != thesis.identity.thesis_id
            or position.symbol != market.symbol
            or position.contract_epoch_id != market.contract_epoch_id):
        raise ValueError("position, thesis, and market identity mismatch")
    if position.last_confirmed_at > decision_at:
        return _decision(
            ManagementAction.UNRESOLVED, ManagementReason.POSITION_EVIDENCE_FUTURE,
            position=position, thesis=thesis, policy=policy, decision_at=decision_at,
        )
    if thesis.events[-1].decision_at > decision_at:
        return _decision(
            ManagementAction.UNRESOLVED, ManagementReason.THESIS_EVIDENCE_FUTURE,
            position=position, thesis=thesis, policy=policy, decision_at=decision_at,
        )
    if thesis.status is ThesisStatus.INVALIDATED:
        return _decision(
            ManagementAction.EXIT_SIGNAL, ManagementReason.THESIS_EXPLICITLY_INVALIDATED,
            position=position, thesis=thesis, policy=policy,
            decision_at=decision_at,
        )
    if market.available_at > decision_at:
        return _decision(
            ManagementAction.UNRESOLVED, ManagementReason.MARKET_OBSERVATION_FUTURE,
            position=position, thesis=thesis, policy=policy, decision_at=decision_at,
        )
    if ((position.side is Side.LONG and market.price <= position.current_effective_stop)
            or (position.side is Side.SHORT and market.price >= position.current_effective_stop)):
        return _decision(
            ManagementAction.REVIEW_REQUIRED,
            ManagementReason.CURRENT_PRICE_BEYOND_EFFECTIVE_STOP,
            position=position, thesis=thesis, policy=policy,
            decision_at=decision_at,
        )
    if adverse_choch is not None:
        if (not isinstance(adverse_choch, R2BStructuralEvaluation)
                or adverse_choch.outcome is not R2BOutcome.CHOCH_CONFIRMED
                or adverse_choch.slot is None
                or adverse_choch.market_event_id is None):
            raise ValueError("typed confirmed CHoCH required")
        if adverse_choch.decision_at > decision_at:
            return _decision(
                ManagementAction.UNRESOLVED, ManagementReason.STRUCTURE_EVIDENCE_FUTURE,
                position=position, thesis=thesis, policy=policy,
                decision_at=decision_at,
            )
        if (adverse_choch.slot.symbol != position.symbol
                or adverse_choch.slot.epoch_id != thesis.identity.epoch_id):
            return _decision(
                ManagementAction.UNRESOLVED,
                ManagementReason.STRUCTURE_IDENTITY_MISMATCH,
                position=position, thesis=thesis, policy=policy,
                decision_at=decision_at,
                market_event_id=adverse_choch.market_event_id,
            )
        if adverse_choch.slot.side is position.side:
            return _decision(
                ManagementAction.HOLD, ManagementReason.IRRELEVANT_CHOCH_DIRECTION,
                position=position, thesis=thesis, policy=policy,
                decision_at=decision_at,
                market_event_id=adverse_choch.market_event_id,
            )
        return _decision(
            ManagementAction.REVIEW_REQUIRED,
            ManagementReason.CHOCH_NO_AUTOMATIC_THESIS_EFFECT,
            position=position, thesis=thesis, policy=policy,
            decision_at=decision_at,
            market_event_id=adverse_choch.market_event_id,
        )
    if protection is None:
        return _decision(
            ManagementAction.HOLD, ManagementReason.NO_NEW_PROTECTED_STRUCTURE,
            position=position, thesis=thesis, policy=policy,
            decision_at=decision_at,
        )
    level = protection.level
    continuation = protection.continuation
    expected_relation = (
        InvalidationRelation.BREAK_BELOW if position.side is Side.LONG
        else InvalidationRelation.BREAK_ABOVE
    )
    if (level.thesis_id != position.thesis_id
            or level.side is not position.side
            or level.scope is not InvalidationScope.THESIS
            or level.relation is not expected_relation
            or continuation.slot.symbol != position.symbol
            or continuation.slot.side is not position.side
            or continuation.slot.epoch_id != thesis.identity.epoch_id):
        return _decision(
            ManagementAction.UNRESOLVED,
            ManagementReason.STRUCTURE_IDENTITY_MISMATCH,
            position=position, thesis=thesis, policy=policy,
            decision_at=decision_at, protection=protection,
            market_event_id=continuation.market_event_id,
        )
    if level.times.available_at > decision_at or continuation.decision_at > decision_at:
        return _decision(
            ManagementAction.UNRESOLVED, ManagementReason.STRUCTURE_EVIDENCE_FUTURE,
            position=position, thesis=thesis, policy=policy,
            decision_at=decision_at, protection=protection,
            market_event_id=continuation.market_event_id,
        )
    stop = build_initial_stop_plan(
        thesis_id=position.thesis_id, side=position.side,
        entry_price=market.price, invalidations=(level,),
        required_scope=InvalidationScope.THESIS,
        selected_level_id=level.level_id, atr=atr, tick_size=tick_size,
        spread=spread, policy=stop_policy, decision_at=decision_at,
    )
    if stop.status is not PlanStatus.READY:
        return _decision(
            ManagementAction.UNRESOLVED, ManagementReason.STOP_PLAN_UNRESOLVED,
            position=position, thesis=thesis, policy=policy,
            decision_at=decision_at, protection=protection,
            market_event_id=continuation.market_event_id,
        )
    improved = (
        stop.final_stop > position.current_effective_stop
        if position.side is Side.LONG
        else stop.final_stop < position.current_effective_stop
    )
    if not improved:
        return _decision(
            ManagementAction.HOLD, ManagementReason.STOP_NOT_IMPROVED,
            position=position, thesis=thesis, policy=policy,
            decision_at=decision_at, protection=protection,
            market_event_id=continuation.market_event_id,
        )
    return _decision(
        ManagementAction.RATCHET_CANDIDATE, None,
        position=position, thesis=thesis, policy=policy,
        decision_at=decision_at, proposed_stop=stop.final_stop,
        buffer=stop.buffer, protection=protection,
        market_event_id=continuation.market_event_id,
    )
