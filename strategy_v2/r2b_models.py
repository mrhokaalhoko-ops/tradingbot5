"""Pure R2B structural research contracts with no execution authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .causality import require_utc
from .enums import Side
from .events import canonical_sha256
from .structure_models import ConfirmedPivot


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _digest(value: str, name: str, length: int = 64) -> None:
    if not isinstance(value, str) or len(value) != length or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ValueError(f"{name} must be a {length}-character hex digest")


class R2BEvaluationFamily(str, Enum):
    INITIAL_BREAK = "INITIAL_BREAK_EVAL"
    CONTINUATION_BOS = "CONTINUATION_BOS_EVAL"
    PROTECTED_CLOSE_CROSS = "PROTECTED_CLOSE_CROSS_EVAL"


class R2BOutcome(str, Enum):
    INITIAL_BREAK_CONFIRMED = "INITIAL_BREAK_CONFIRMED"
    CONTINUATION_BOS_CONFIRMED = "CONTINUATION_BOS_CONFIRMED"
    CHOCH_CONFIRMED = "CHOCH_CONFIRMED"
    FINAL_NO_CROSS = "FINAL_NO_CROSS"
    PROMOTION_ELIGIBLE_CANDIDATE = "PROMOTION_ELIGIBLE_CANDIDATE"
    UNRESOLVED = "UNRESOLVED"
    DISQUALIFIED = "DISQUALIFIED"


class R2BReason(str, Enum):
    INVALID_INPUT_TYPE = "INVALID_INPUT_TYPE"
    MIXED_CONTEXT = "MIXED_CONTEXT"
    FUTURE_EVIDENCE = "FUTURE_EVIDENCE"
    EPOCH_INCOMPLETE = "EPOCH_INCOMPLETE"
    CANDIDATE_BAR_NOT_FOUND = "CANDIDATE_BAR_NOT_FOUND"
    STRUCTURE_ROLE_UNRESOLVED = "STRUCTURE_ROLE_UNRESOLVED"
    ROLE_PATTERN_INVALID = "ROLE_PATTERN_INVALID"
    REFERENCE_NOT_UNIQUE_EXTREME = "REFERENCE_NOT_UNIQUE_EXTREME"
    ROLE_NOT_YET_KNOWABLE = "ROLE_NOT_YET_KNOWABLE"
    INCOMPLETE_FIRST_CROSS_HISTORY = "INCOMPLETE_FIRST_CROSS_HISTORY"
    REFERENCE_PREVIOUSLY_BROKEN = "REFERENCE_PREVIOUSLY_BROKEN"
    CONTEXT_GUARD_BROKEN = "CONTEXT_GUARD_BROKEN"
    REPEATED_INITIAL_CROSS = "REPEATED_INITIAL_CROSS"
    NUMERIC_VARIANT_MISMATCH = "NUMERIC_VARIANT_MISMATCH"
    NUMERIC_REFERENCE_MISMATCH = "NUMERIC_REFERENCE_MISMATCH"
    NUMERIC_DIRECTION_MISMATCH = "NUMERIC_DIRECTION_MISMATCH"
    NUMERIC_EVIDENCE_UNRESOLVED = "NUMERIC_EVIDENCE_UNRESOLVED"
    NO_QUALIFYING_PIVOT = "NO_QUALIFYING_PIVOT"
    MULTIPLE_CANDIDATE_LEVELS = "MULTIPLE_CANDIDATE_LEVELS"
    PIVOT_OUTSIDE_FORMATION_LEG = "PIVOT_OUTSIDE_FORMATION_LEG"
    PIVOT_NOT_AVAILABLE_BEFORE_BREAK = "PIVOT_NOT_AVAILABLE_BEFORE_BREAK"
    PROTECTION_CANDIDATE_BREACHED = "PROTECTION_CANDIDATE_BREACHED"
    INCOMPLETE_PROTECTION_PATH = "INCOMPLETE_PROTECTION_PATH"
    COMPETING_REFERENCE = "COMPETING_REFERENCE"
    COUNTER_EVENT = "COUNTER_EVENT"
    INCOMPLETE_COUNTER_EVENT_HISTORY = "INCOMPLETE_COUNTER_EVENT_HISTORY"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    INITIAL_EVENT_NOT_CONFIRMED = "INITIAL_EVENT_NOT_CONFIRMED"
    NO_CONTINUATION_REFERENCE = "NO_CONTINUATION_REFERENCE"
    NO_CONTINUATION_RETRACE = "NO_CONTINUATION_RETRACE"
    REPEATED_CONTINUATION_CROSS = "REPEATED_CONTINUATION_CROSS"
    NO_POST_ACTIVATION_PREVIOUS_CLOSE = "NO_POST_ACTIVATION_PREVIOUS_CLOSE"
    INCOMPLETE_FIRST_BREACH_HISTORY = "INCOMPLETE_FIRST_BREACH_HISTORY"
    PRE_ELIGIBLE_GUARD_BREACH = "PRE_ELIGIBLE_GUARD_BREACH"
    GUARD_BREACH_PENDING_DISPOSITION = "GUARD_BREACH_PENDING_DISPOSITION"
    NO_ACTIVE_STRUCTURAL_GUARD = "NO_ACTIVE_STRUCTURAL_GUARD"


@dataclass(frozen=True, slots=True)
class R2BVariant:
    """Complete structural namespace layered on one immutable R2A numeric variant."""

    source_commit: str
    schema_version: str
    rule_version: str
    variant_id: str
    symbol: str
    timeframe: str
    verified_feed_basis_id: str
    r2a_variant_hash: str
    epoch_policy_id: str
    initial_role_policy_id: str
    first_cross_policy_id: str
    continuation_policy_id: str
    counter_event_policy_id: str
    protection_policy_id: str
    slot_policy_id: str
    semantic_snapshot_policy_id: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        _digest(self.r2a_variant_hash, "r2a_variant_hash")
        for name in (
            "schema_version", "rule_version", "variant_id", "symbol",
            "timeframe", "verified_feed_basis_id", "epoch_policy_id",
            "initial_role_policy_id", "first_cross_policy_id",
            "continuation_policy_id", "counter_event_policy_id",
            "protection_policy_id", "slot_policy_id",
            "semantic_snapshot_policy_id",
        ):
            _text(getattr(self, name), name)
        identity = tuple(
            getattr(self, name) for name in self.__dataclass_fields__
            if name not in ("variant_hash", "execution_authority")
        )
        object.__setattr__(self, "variant_hash", canonical_sha256(identity).lower())


@dataclass(frozen=True, slots=True)
class FrozenStructuralEpoch:
    symbol: str
    timeframe: str
    start_at: datetime
    end_at: datetime
    witness_available_at: datetime
    dataset_hash: str
    seed_coverage_hash: str
    complete_prefix: bool
    variant_hash: str
    epoch_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("symbol", "timeframe"):
            _text(getattr(self, name), name)
        for name in ("start_at", "end_at", "witness_available_at"):
            require_utc(getattr(self, name), name)
        if not self.witness_available_at <= self.start_at < self.end_at:
            raise ValueError("epoch must be registered no later than start_at and have a positive range")
        for name in ("dataset_hash", "seed_coverage_hash", "variant_hash"):
            _digest(getattr(self, name), name)
        if type(self.complete_prefix) is not bool:
            raise TypeError("complete_prefix must be bool")
        object.__setattr__(self, "epoch_id", "epc_" + canonical_sha256((
            self.symbol, self.timeframe, self.start_at, self.end_at,
            self.witness_available_at, self.dataset_hash,
            self.seed_coverage_hash, self.complete_prefix, self.variant_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class InitialStructuralRole:
    side: Side
    pivot_ids: tuple[str, str, str, str]
    reference_pivot_id: str
    contextual_pivot_id: str
    role_first_knowable_at: datetime
    first_eligible_bar_id: str
    first_eligible_bar_open_at: datetime
    epoch_id: str
    variant_hash: str
    role_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise TypeError("side must be Side")
        if len(self.pivot_ids) != 4 or len(set(self.pivot_ids)) != 4 or any(
            not isinstance(value, str) or not value for value in self.pivot_ids
        ):
            raise ValueError("four unique pivot IDs required")
        if self.reference_pivot_id not in self.pivot_ids or self.contextual_pivot_id not in self.pivot_ids:
            raise ValueError("reference and contextual pivots must belong to role")
        require_utc(self.role_first_knowable_at, "role_first_knowable_at")
        require_utc(self.first_eligible_bar_open_at, "first_eligible_bar_open_at")
        if self.first_eligible_bar_open_at <= self.role_first_knowable_at:
            raise ValueError("first eligible bar must open strictly after role is knowable")
        _text(self.first_eligible_bar_id, "first_eligible_bar_id")
        _text(self.epoch_id, "epoch_id")
        _digest(self.variant_hash, "variant_hash")
        object.__setattr__(self, "role_id", "rol_" + canonical_sha256((
            self.side, self.pivot_ids, self.reference_pivot_id,
            self.contextual_pivot_id, self.role_first_knowable_at,
            self.first_eligible_bar_id, self.first_eligible_bar_open_at,
            self.epoch_id, self.variant_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class InitialRoleResolution:
    role: InitialStructuralRole | None
    reason: R2BReason | None
    evidence_ids: tuple[str, ...]
    resolution_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if (self.role is None) == (self.reason is None):
            raise ValueError("resolution needs exactly one role or reason")
        if self.reason is not None and not isinstance(self.reason, R2BReason):
            raise TypeError("typed reason required")
        if not isinstance(self.evidence_ids, tuple) or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("unique evidence IDs required")
        object.__setattr__(self, "resolution_id", "rrs_" + canonical_sha256((
            self.role, self.reason, self.evidence_ids,
        )).lower())


@dataclass(frozen=True, slots=True)
class ContinuationStructuralRole:
    side: Side
    initial_event_id: str
    initial_break_bar_id: str
    initial_reference_pivot_id: str
    counter_reference_pivot_id: str
    reference_pivot_id: str
    retrace_pivot_ids: tuple[str, ...]
    role_first_knowable_at: datetime
    first_eligible_bar_id: str
    first_eligible_bar_open_at: datetime
    epoch_id: str
    variant_hash: str
    role_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise TypeError("typed side required")
        for name in ("initial_event_id", "initial_break_bar_id",
                     "initial_reference_pivot_id", "counter_reference_pivot_id",
                     "reference_pivot_id", "first_eligible_bar_id", "epoch_id"):
            _text(getattr(self, name), name)
        if not self.retrace_pivot_ids or len(set(self.retrace_pivot_ids)) != len(self.retrace_pivot_ids):
            raise ValueError("one or more unique retrace pivots required")
        require_utc(self.role_first_knowable_at, "role_first_knowable_at")
        require_utc(self.first_eligible_bar_open_at, "first_eligible_bar_open_at")
        if self.first_eligible_bar_open_at <= self.role_first_knowable_at:
            raise ValueError("continuation eligibility must begin after role knowledge")
        _digest(self.variant_hash, "variant_hash")
        object.__setattr__(self, "role_id", "crl_" + canonical_sha256((
            self.side, self.initial_event_id, self.initial_break_bar_id,
            self.initial_reference_pivot_id, self.counter_reference_pivot_id,
            self.reference_pivot_id, self.retrace_pivot_ids,
            self.role_first_knowable_at, self.first_eligible_bar_id,
            self.first_eligible_bar_open_at, self.epoch_id, self.variant_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class ContinuationRoleResolution:
    role: ContinuationStructuralRole | None
    reason: R2BReason | None
    evidence_ids: tuple[str, ...]
    resolution_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if (self.role is None) == (self.reason is None):
            raise ValueError("resolution needs exactly one role or reason")
        if self.reason is not None and not isinstance(self.reason, R2BReason):
            raise TypeError("typed reason required")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("unique evidence IDs required")
        object.__setattr__(self, "resolution_id", "crs_" + canonical_sha256((
            self.role, self.reason, self.evidence_ids,
        )).lower())


@dataclass(frozen=True, slots=True)
class MarketSlotIdentity:
    evaluation_family: R2BEvaluationFamily
    symbol: str
    timeframe: str
    epoch_id: str
    reference_id: str
    bar_id: str
    side: Side
    verified_feed_basis_id: str
    variant_hash: str
    guard_id: str | None = None
    protection_epoch_id: str | None = None
    market_slot_key: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_family, R2BEvaluationFamily):
            raise TypeError("typed evaluation family required")
        if not isinstance(self.side, Side):
            raise TypeError("typed side required")
        for name in ("symbol", "timeframe", "epoch_id", "reference_id",
                     "bar_id", "verified_feed_basis_id"):
            _text(getattr(self, name), name)
        _digest(self.variant_hash, "variant_hash")
        protected = self.evaluation_family is R2BEvaluationFamily.PROTECTED_CLOSE_CROSS
        if protected != (self.guard_id is not None and self.protection_epoch_id is not None):
            raise ValueError("protected cross slots require explicit guard and protection epoch only")
        if protected:
            _text(self.guard_id, "guard_id")
            _text(self.protection_epoch_id, "protection_epoch_id")
        object.__setattr__(self, "market_slot_key", "slt_" + canonical_sha256((
            self.evaluation_family, self.symbol, self.timeframe, self.epoch_id,
            self.reference_id, self.bar_id, self.side,
            self.verified_feed_basis_id, self.variant_hash,
            self.guard_id, self.protection_epoch_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class R2BStructuralEvaluation:
    outcome: R2BOutcome
    reason: R2BReason | None
    slot: MarketSlotIdentity | None
    role_id: str | None
    parent_evidence_ids: tuple[str, ...]
    source_hash: str
    decision_at: datetime
    market_event_id: str | None = field(init=False)
    evaluation_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, R2BOutcome):
            raise TypeError("typed outcome required")
        if self.reason is not None and not isinstance(self.reason, R2BReason):
            raise TypeError("typed reason required")
        require_utc(self.decision_at, "decision_at")
        if not isinstance(self.parent_evidence_ids, tuple) or len(set(self.parent_evidence_ids)) != len(self.parent_evidence_ids):
            raise ValueError("unique parent evidence IDs required")
        _digest(self.source_hash, "source_hash")
        final = self.outcome in (
            R2BOutcome.INITIAL_BREAK_CONFIRMED,
            R2BOutcome.CONTINUATION_BOS_CONFIRMED,
            R2BOutcome.CHOCH_CONFIRMED,
            R2BOutcome.FINAL_NO_CROSS,
        )
        if final and (self.slot is None or self.reason is not None):
            raise ValueError("final result needs a slot and no rejection reason")
        if not final and self.reason is None:
            raise ValueError("non-final result needs a typed reason")
        market_event_id = None
        if final:
            market_event_id = "mkt_" + canonical_sha256((
                self.slot.market_slot_key, self.outcome, self.source_hash,
            )).lower()
        object.__setattr__(self, "market_event_id", market_event_id)
        object.__setattr__(self, "evaluation_id", "evl_" + canonical_sha256((
            self.outcome, self.reason, self.slot, self.role_id,
            self.parent_evidence_ids, self.source_hash, self.decision_at,
        )).lower())


@dataclass(frozen=True, slots=True)
class ProtectionCandidateAssessment:
    pivot: ConfirmedPivot
    qualified: bool
    reason: R2BReason | None
    assessment_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.pivot) is not ConfirmedPivot:
            raise TypeError("confirmed pivot required")
        if self.qualified == (self.reason is not None):
            raise ValueError("qualified candidate has no reason; rejected candidate has one")
        object.__setattr__(self, "assessment_id", "qas_" + canonical_sha256((
            self.pivot.pivot_id, self.qualified, self.reason,
        )).lower())


@dataclass(frozen=True, slots=True)
class ProtectionCandidateLedger:
    continuation_event_id: str
    break_bar_id: str
    assessments: tuple[ProtectionCandidateAssessment, ...]
    outcome: R2BOutcome
    reason: R2BReason | None
    selected_pivot_id: str | None
    epoch_id: str
    variant_hash: str
    ledger_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("continuation_event_id", "break_bar_id", "epoch_id"):
            _text(getattr(self, name), name)
        _digest(self.variant_hash, "variant_hash")
        if not isinstance(self.assessments, tuple) or len({a.pivot.pivot_id for a in self.assessments}) != len(self.assessments):
            raise ValueError("unique candidate assessments required")
        qualified = tuple(a for a in self.assessments if a.qualified)
        if len(qualified) == 1:
            if (self.outcome is not R2BOutcome.PROMOTION_ELIGIBLE_CANDIDATE or
                    self.reason is not None or
                    self.selected_pivot_id != qualified[0].pivot.pivot_id):
                raise ValueError("one qualified pivot must be the sole promotion candidate")
        else:
            expected = (R2BReason.NO_QUALIFYING_PIVOT if not qualified
                        else R2BReason.MULTIPLE_CANDIDATE_LEVELS)
            if (self.outcome is not R2BOutcome.UNRESOLVED or
                    self.reason is not expected or self.selected_pivot_id is not None):
                raise ValueError("UNIQUE_ONLY resolution mismatch")
        object.__setattr__(self, "ledger_id", "qld_" + canonical_sha256((
            self.continuation_event_id, self.break_bar_id, self.assessments,
            self.outcome, self.reason, self.selected_pivot_id,
            self.epoch_id, self.variant_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class SimulatedResearchGuard:
    side: Side
    level_pivot_id: str
    level_price: Decimal
    continuation_event_id: str
    market_active_from: datetime
    symbol: str
    timeframe: str
    epoch_id: str
    variant_hash: str
    protection_epoch_id: str
    guard_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise TypeError("typed side required")
        if not isinstance(self.level_price, Decimal) or not self.level_price.is_finite() or self.level_price <= 0:
            raise ValueError("positive finite guard price required")
        for name in ("level_pivot_id", "continuation_event_id", "symbol",
                     "timeframe", "epoch_id", "protection_epoch_id"):
            _text(getattr(self, name), name)
        require_utc(self.market_active_from, "market_active_from")
        _digest(self.variant_hash, "variant_hash")
        object.__setattr__(self, "guard_id", "grd_" + canonical_sha256((
            self.side, self.level_pivot_id, self.level_price,
            self.continuation_event_id, self.market_active_from, self.symbol,
            self.timeframe, self.epoch_id, self.variant_hash,
            self.protection_epoch_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class BarIneligibilityDisposition:
    guard_id: str
    protection_epoch_id: str
    symbol: str
    timeframe: str
    bar_id: str
    variant_hash: str
    reason: R2BReason
    disposition_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("guard_id", "protection_epoch_id", "symbol", "timeframe", "bar_id"):
            _text(getattr(self, name), name)
        _digest(self.variant_hash, "variant_hash")
        if self.reason is not R2BReason.NO_POST_ACTIVATION_PREVIOUS_CLOSE:
            raise ValueError("this disposition is only for a missing post-activation baseline")
        object.__setattr__(self, "disposition_id", "inl_" + canonical_sha256((
            self.guard_id, self.protection_epoch_id, self.symbol,
            self.timeframe, self.bar_id, self.variant_hash, self.reason,
        )).lower())


@dataclass(frozen=True, slots=True)
class ProtectedCrossResolution:
    evaluation: R2BStructuralEvaluation
    ineligibility: BarIneligibilityDisposition | None
    guard_retired: bool
    resolution_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation, R2BStructuralEvaluation):
            raise TypeError("structural evaluation required")
        if self.ineligibility is not None and not isinstance(
            self.ineligibility, BarIneligibilityDisposition
        ):
            raise TypeError("typed ineligibility disposition required")
        if type(self.guard_retired) is not bool:
            raise TypeError("guard_retired must be bool")
        if self.ineligibility is not None and self.evaluation.outcome is not R2BOutcome.UNRESOLVED:
            raise ValueError("ineligible bar cannot be a final protected-cross result")
        object.__setattr__(self, "resolution_id", "pcr_" + canonical_sha256((
            self.evaluation, self.ineligibility, self.guard_retired,
        )).lower())
