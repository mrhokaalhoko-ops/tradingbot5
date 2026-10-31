"""Pure Strategy V2 thesis lifecycle and causal reset contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .causality import require_utc
from .enums import InvalidationRelation, InvalidationScope, Side
from .events import canonical_sha256
from .models import InvalidationLevel, ThesisId
from .r2b_models import R2BEvaluationFamily, R2BOutcome, R2BStructuralEvaluation


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _digest(value: str, name: str, length: int = 64) -> None:
    if not isinstance(value, str) or len(value) != length or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ValueError(f"{name} must be a {length}-character hex digest")


def _unique_text_tuple(value: tuple[str, ...], name: str) -> None:
    if not isinstance(value, tuple) or not value or len(set(value)) != len(value):
        raise ValueError(f"{name} must contain one or more unique values")
    for item in value:
        _text(item, name)


class ThesisStatus(str, Enum):
    OBSERVED = "OBSERVED"
    QUALIFIED = "QUALIFIED"
    WAIT_TRIGGER = "WAIT_TRIGGER"
    PROPOSED = "PROPOSED"
    ENTRY_PENDING = "ENTRY_PENDING"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class ThesisEffectPolicy(str, Enum):
    NONE = "THESIS_EFFECT_NONE_V1"
    EXPLICIT_INVALIDATION = "EXPLICIT_THESIS_INVALIDATION_V1"


class ThesisTransitionStatus(str, Enum):
    APPLIED = "APPLIED"
    IDEMPOTENT = "IDEMPOTENT"
    REJECTED = "REJECTED"


class ThesisTransitionReason(str, Enum):
    INVALID_TRANSITION = "INVALID_TRANSITION"
    TERMINAL_STATE = "TERMINAL_STATE"
    FUTURE_EVIDENCE = "FUTURE_EVIDENCE"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    EXPLICIT_INVALIDATION_REQUIRED = "EXPLICIT_INVALIDATION_REQUIRED"
    CHOCH_HAS_NO_AUTOMATIC_THESIS_EFFECT = "CHOCH_HAS_NO_AUTOMATIC_THESIS_EFFECT"


class ReentryClassification(str, Enum):
    NEW_THESIS = "NEW_THESIS"
    SAME_THESIS_VALID_RESET = "SAME_THESIS_VALID_RESET"
    SAME_THESIS_NO_RESET = "SAME_THESIS_NO_RESET"
    CONTEXT_INVALIDATED = "CONTEXT_INVALIDATED"


@dataclass(frozen=True, slots=True)
class ThesisPolicySpec:
    source_commit: str
    schema_version: str
    rule_version: str
    policy_id: str
    r2b_variant_hash: str
    lifecycle_policy_id: str
    reset_policy_id: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        _digest(self.r2b_variant_hash, "r2b_variant_hash")
        for name in ("schema_version", "rule_version", "policy_id",
                     "lifecycle_policy_id", "reset_policy_id"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.schema_version, self.rule_version,
            self.policy_id, self.r2b_variant_hash, self.lifecycle_policy_id,
            self.reset_policy_id,
        )).lower())


def derive_thesis_id(
    *,
    symbol: str,
    side: Side,
    setup_family: str,
    epoch_id: str,
    initial_break_event_id: str,
    htf_context_snapshot_hash: str,
    causal_zone_ids: tuple[str, ...],
    policy: ThesisPolicySpec,
) -> ThesisId:
    for name, value in (
        ("symbol", symbol), ("setup_family", setup_family),
        ("epoch_id", epoch_id), ("initial_break_event_id", initial_break_event_id),
    ):
        _text(value, name)
    if not isinstance(side, Side):
        raise TypeError("typed side required")
    _digest(htf_context_snapshot_hash, "htf_context_snapshot_hash")
    _unique_text_tuple(causal_zone_ids, "causal_zone_ids")
    if not isinstance(policy, ThesisPolicySpec):
        raise TypeError("typed thesis policy required")
    return ThesisId("ths_" + canonical_sha256((
        symbol, side, setup_family, epoch_id, initial_break_event_id,
        htf_context_snapshot_hash, causal_zone_ids, policy.variant_hash,
    )).lower())


@dataclass(frozen=True, slots=True)
class ThesisIdentity:
    thesis_id: ThesisId
    symbol: str
    side: Side
    setup_family: str
    epoch_id: str
    initial_break_event_id: str
    htf_context_snapshot_hash: str
    causal_zone_ids: tuple[str, ...]
    explicit_invalidation_level_id: str
    policy_variant_hash: str
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.thesis_id) is not ThesisId or not isinstance(self.side, Side):
            raise TypeError("typed thesis ID and side required")
        for name in ("symbol", "setup_family", "epoch_id",
                     "initial_break_event_id", "explicit_invalidation_level_id"):
            _text(getattr(self, name), name)
        for name in ("htf_context_snapshot_hash", "policy_variant_hash"):
            _digest(getattr(self, name), name)
        _unique_text_tuple(self.causal_zone_ids, "causal_zone_ids")


@dataclass(frozen=True, slots=True)
class ThesisTransitionEvent:
    thesis_id: ThesisId
    from_status: ThesisStatus | None
    to_status: ThesisStatus
    event_at: datetime
    decision_at: datetime
    evidence_ids: tuple[str, ...]
    reason_code: str
    effect_policy: ThesisEffectPolicy
    explicit_invalidation_event_id: str | None = None
    event_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.thesis_id) is not ThesisId:
            raise TypeError("typed thesis ID required")
        if self.from_status is not None and not isinstance(self.from_status, ThesisStatus):
            raise TypeError("typed source status required")
        if not isinstance(self.to_status, ThesisStatus) or not isinstance(
            self.effect_policy, ThesisEffectPolicy
        ):
            raise TypeError("typed target status and effect policy required")
        require_utc(self.event_at, "event_at")
        require_utc(self.decision_at, "decision_at")
        if self.event_at > self.decision_at:
            raise ValueError("transition event cannot be known before it occurs")
        _unique_text_tuple(self.evidence_ids, "evidence_ids")
        _text(self.reason_code, "reason_code")
        if self.explicit_invalidation_event_id is not None:
            _text(self.explicit_invalidation_event_id, "explicit_invalidation_event_id")
        if self.to_status is ThesisStatus.INVALIDATED:
            if (self.effect_policy is not ThesisEffectPolicy.EXPLICIT_INVALIDATION
                    or self.explicit_invalidation_event_id is None):
                raise ValueError("explicit invalidation event and policy required")
        elif self.explicit_invalidation_event_id is not None:
            raise ValueError("only invalidation transitions may carry invalidation event ID")
        object.__setattr__(self, "event_id", "thv_" + canonical_sha256((
            self.thesis_id, self.from_status, self.to_status, self.event_at,
            self.evidence_ids, self.reason_code, self.effect_policy,
            self.explicit_invalidation_event_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class ThesisSnapshot:
    identity: ThesisIdentity
    status: ThesisStatus
    created_at: datetime
    last_event_at: datetime
    events: tuple[ThesisTransitionEvent, ...]
    snapshot_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ThesisIdentity) or not isinstance(self.status, ThesisStatus):
            raise TypeError("typed thesis identity and status required")
        require_utc(self.created_at, "created_at")
        require_utc(self.last_event_at, "last_event_at")
        if not self.events or any(type(event) is not ThesisTransitionEvent for event in self.events):
            raise ValueError("one or more typed thesis events required")
        if self.events[0].from_status is not None or self.events[0].to_status is not ThesisStatus.OBSERVED:
            raise ValueError("first thesis event must create OBSERVED state")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("duplicate thesis events forbidden")
        for index, event in enumerate(self.events):
            if event.thesis_id != self.identity.thesis_id:
                raise ValueError("event thesis identity mismatch")
            if index:
                previous = self.events[index - 1]
                if event.from_status is not previous.to_status:
                    raise ValueError("thesis event chain is discontinuous")
                if event.to_status not in _ALLOWED_TRANSITIONS[previous.to_status]:
                    raise ValueError("thesis event chain contains invalid transition")
                if (event.event_at < previous.event_at
                        or event.decision_at < previous.decision_at):
                    raise ValueError("thesis event chronology is not monotonic")
        if self.events[-1].to_status is not self.status:
            raise ValueError("snapshot status must match final event")
        if self.created_at != self.events[0].event_at or self.last_event_at != self.events[-1].event_at:
            raise ValueError("snapshot event times disagree")
        object.__setattr__(self, "snapshot_id", "thsnp_" + canonical_sha256((
            self.identity, self.status, self.created_at, self.last_event_at,
            tuple(event.event_id for event in self.events),
        )).lower())


@dataclass(frozen=True, slots=True)
class ThesisTransitionResult:
    status: ThesisTransitionStatus
    snapshot: ThesisSnapshot
    reason: ThesisTransitionReason | None
    result_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, ThesisTransitionStatus):
            raise TypeError("typed transition result required")
        if self.status is ThesisTransitionStatus.REJECTED:
            if not isinstance(self.reason, ThesisTransitionReason):
                raise ValueError("rejected transition needs typed reason")
        elif self.reason is not None:
            raise ValueError("successful transition cannot carry rejection reason")
        object.__setattr__(self, "result_id", "thr_" + canonical_sha256((
            self.status, self.snapshot.snapshot_id, self.reason,
        )).lower())


_ALLOWED_TRANSITIONS = {
    ThesisStatus.OBSERVED: {ThesisStatus.QUALIFIED, ThesisStatus.INVALIDATED, ThesisStatus.EXPIRED},
    ThesisStatus.QUALIFIED: {ThesisStatus.WAIT_TRIGGER, ThesisStatus.INVALIDATED, ThesisStatus.EXPIRED},
    ThesisStatus.WAIT_TRIGGER: {ThesisStatus.PROPOSED, ThesisStatus.INVALIDATED, ThesisStatus.EXPIRED},
    ThesisStatus.PROPOSED: {ThesisStatus.ENTRY_PENDING, ThesisStatus.WAIT_TRIGGER,
                            ThesisStatus.INVALIDATED, ThesisStatus.EXPIRED},
    ThesisStatus.ENTRY_PENDING: {ThesisStatus.ACTIVE, ThesisStatus.WAIT_TRIGGER,
                                 ThesisStatus.INVALIDATED, ThesisStatus.EXPIRED},
    ThesisStatus.ACTIVE: {ThesisStatus.CLOSED, ThesisStatus.INVALIDATED},
    ThesisStatus.CLOSED: set(),
    ThesisStatus.INVALIDATED: set(),
    ThesisStatus.EXPIRED: set(),
}


def create_thesis(
    *,
    initial_break: R2BStructuralEvaluation,
    symbol: str,
    side: Side,
    setup_family: str,
    epoch_id: str,
    htf_context_snapshot_hash: str,
    causal_zone_ids: tuple[str, ...],
    invalidation: InvalidationLevel,
    policy: ThesisPolicySpec,
    decision_at: datetime,
) -> ThesisSnapshot:
    require_utc(decision_at, "decision_at")
    if (not isinstance(initial_break, R2BStructuralEvaluation)
            or initial_break.outcome is not R2BOutcome.INITIAL_BREAK_CONFIRMED
            or initial_break.market_event_id is None
            or initial_break.slot is None):
        raise ValueError("confirmed initial break required")
    slot = initial_break.slot
    if (slot.evaluation_family is not R2BEvaluationFamily.INITIAL_BREAK
            or slot.symbol != symbol or slot.side is not side
            or slot.epoch_id != epoch_id
            or slot.variant_hash != policy.r2b_variant_hash):
        raise ValueError("initial break provenance does not match thesis inputs")
    thesis_id = derive_thesis_id(
        symbol=symbol, side=side, setup_family=setup_family, epoch_id=epoch_id,
        initial_break_event_id=initial_break.market_event_id,
        htf_context_snapshot_hash=htf_context_snapshot_hash,
        causal_zone_ids=causal_zone_ids, policy=policy,
    )
    if (not isinstance(invalidation, InvalidationLevel)
            or invalidation.scope is not InvalidationScope.THESIS
            or invalidation.thesis_id != thesis_id
            or invalidation.side is not side
            or invalidation.relation is not (
                InvalidationRelation.BREAK_BELOW if side is Side.LONG
                else InvalidationRelation.BREAK_ABOVE
            )
            or invalidation.times.available_at > decision_at):
        raise ValueError("causal thesis invalidation bound to derived thesis is required")
    identity = ThesisIdentity(
        thesis_id, symbol, side, setup_family, epoch_id,
        initial_break.market_event_id, htf_context_snapshot_hash,
        causal_zone_ids, invalidation.level_id, policy.variant_hash,
    )
    evidence = tuple(dict.fromkeys((
        initial_break.market_event_id, invalidation.level_id, *causal_zone_ids,
    )))
    created_at = max(initial_break.decision_at, invalidation.times.available_at)
    if created_at > decision_at:
        raise ValueError("future thesis evidence forbidden")
    event = ThesisTransitionEvent(
        thesis_id, None, ThesisStatus.OBSERVED, created_at, decision_at,
        evidence, "INITIAL_BREAK_AND_CAUSAL_INVALIDATION_OBSERVED",
        ThesisEffectPolicy.NONE,
    )
    return ThesisSnapshot(identity, ThesisStatus.OBSERVED, created_at, created_at, (event,))


def transition_thesis(
    snapshot: ThesisSnapshot,
    *,
    target: ThesisStatus,
    event_at: datetime,
    decision_at: datetime,
    evidence_ids: tuple[str, ...],
    reason_code: str,
    effect_policy: ThesisEffectPolicy = ThesisEffectPolicy.NONE,
    explicit_invalidation_event_id: str | None = None,
) -> ThesisTransitionResult:
    if not isinstance(snapshot, ThesisSnapshot) or not isinstance(target, ThesisStatus):
        raise TypeError("typed snapshot and target required")
    require_utc(event_at, "event_at")
    require_utc(decision_at, "decision_at")
    if target is snapshot.status:
        return ThesisTransitionResult(ThesisTransitionStatus.IDEMPOTENT, snapshot, None)
    if snapshot.status in (ThesisStatus.CLOSED, ThesisStatus.INVALIDATED, ThesisStatus.EXPIRED):
        return ThesisTransitionResult(
            ThesisTransitionStatus.REJECTED, snapshot,
            ThesisTransitionReason.TERMINAL_STATE,
        )
    if target not in _ALLOWED_TRANSITIONS[snapshot.status]:
        return ThesisTransitionResult(
            ThesisTransitionStatus.REJECTED, snapshot,
            ThesisTransitionReason.INVALID_TRANSITION,
        )
    if event_at > decision_at or event_at < snapshot.last_event_at:
        return ThesisTransitionResult(
            ThesisTransitionStatus.REJECTED, snapshot,
            ThesisTransitionReason.FUTURE_EVIDENCE,
        )
    if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
        return ThesisTransitionResult(
            ThesisTransitionStatus.REJECTED, snapshot,
            ThesisTransitionReason.MISSING_EVIDENCE,
        )
    if target is ThesisStatus.INVALIDATED and (
        effect_policy is not ThesisEffectPolicy.EXPLICIT_INVALIDATION
        or explicit_invalidation_event_id is None
    ):
        reason = (ThesisTransitionReason.CHOCH_HAS_NO_AUTOMATIC_THESIS_EFFECT
                  if any("choch" in item.lower() for item in evidence_ids)
                  else ThesisTransitionReason.EXPLICIT_INVALIDATION_REQUIRED)
        return ThesisTransitionResult(ThesisTransitionStatus.REJECTED, snapshot, reason)
    event = ThesisTransitionEvent(
        snapshot.identity.thesis_id, snapshot.status, target, event_at,
        decision_at, evidence_ids, reason_code, effect_policy,
        explicit_invalidation_event_id,
    )
    updated = ThesisSnapshot(
        snapshot.identity, target, snapshot.created_at, event_at,
        snapshot.events + (event,),
    )
    return ThesisTransitionResult(ThesisTransitionStatus.APPLIED, updated, None)


@dataclass(frozen=True, slots=True)
class ResetEvidence:
    reset_event_id: str
    thesis_id: ThesisId
    confirmed_at: datetime
    available_at: datetime
    source_event_ids: tuple[str, ...]
    context_valid: bool
    reset_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _text(self.reset_event_id, "reset_event_id")
        if type(self.thesis_id) is not ThesisId:
            raise TypeError("typed thesis ID required")
        require_utc(self.confirmed_at, "confirmed_at")
        require_utc(self.available_at, "available_at")
        if self.confirmed_at > self.available_at:
            raise ValueError("reset cannot be available before confirmation")
        _unique_text_tuple(self.source_event_ids, "source_event_ids")
        if type(self.context_valid) is not bool:
            raise TypeError("context_valid must be bool")
        object.__setattr__(self, "reset_id", "rst_" + canonical_sha256((
            self.reset_event_id, self.thesis_id, self.confirmed_at,
            self.available_at, self.source_event_ids, self.context_valid,
        )).lower())


def classify_reentry(
    *,
    prior_thesis_id: ThesisId,
    candidate_thesis_id: ThesisId,
    prior_close_at: datetime,
    decision_at: datetime,
    reset: ResetEvidence | None,
) -> ReentryClassification:
    if type(prior_thesis_id) is not ThesisId or type(candidate_thesis_id) is not ThesisId:
        raise TypeError("typed thesis IDs required")
    require_utc(prior_close_at, "prior_close_at")
    require_utc(decision_at, "decision_at")
    if candidate_thesis_id != prior_thesis_id:
        return ReentryClassification.NEW_THESIS
    if reset is None or reset.thesis_id != prior_thesis_id:
        return ReentryClassification.SAME_THESIS_NO_RESET
    if not reset.context_valid:
        return ReentryClassification.CONTEXT_INVALIDATED
    if not prior_close_at < reset.confirmed_at <= reset.available_at <= decision_at:
        return ReentryClassification.SAME_THESIS_NO_RESET
    return ReentryClassification.SAME_THESIS_VALID_RESET
