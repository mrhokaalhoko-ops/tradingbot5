"""Pure R8 append-only attempt admission; no order or lifecycle mutation surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .causality import require_utc
from .events import canonical_sha256
from .models import AttemptId, SetupId, ThesisId
from .r3_thesis import ResetEvidence, ReentryClassification, classify_reentry
from .r7_proposal import AtomicResearchProposal


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _unique_text(value: tuple[str, ...], name: str) -> None:
    if (
        not isinstance(value, tuple)
        or not value
        or len(set(value)) != len(value)
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"{name} requires one or more unique values")


class AttemptEventKind(str, Enum):
    ADMITTED = "ADMITTED"
    CLOSED = "CLOSED"


class AttemptAdmissionStatus(str, Enum):
    ADMITTED = "ADMITTED"
    BLOCKED = "BLOCKED"
    UNRESOLVED = "UNRESOLVED"


class AttemptAdmissionReason(str, Enum):
    DUPLICATE_ATTEMPT = "DUPLICATE_ATTEMPT"
    PRIOR_ATTEMPT_OPEN = "PRIOR_ATTEMPT_OPEN"
    SAME_THESIS_RESET_MISSING = "SAME_THESIS_RESET_MISSING"
    CONTEXT_INVALIDATED = "CONTEXT_INVALIDATED"
    HISTORY_INVALID = "HISTORY_INVALID"
    DECISION_TIME_MISMATCH = "DECISION_TIME_MISMATCH"
    RESET_EVIDENCE_FUTURE = "RESET_EVIDENCE_FUTURE"
    EXECUTION_AUTHORITY_PRESENT = "EXECUTION_AUTHORITY_PRESENT"


@dataclass(frozen=True, slots=True)
class AttemptLedgerEvent:
    kind: AttemptEventKind
    thesis_id: ThesisId
    setup_id: SetupId
    attempt_id: AttemptId
    proposal_assembly_id: str
    event_at: datetime
    available_at: datetime
    source_event_ids: tuple[str, ...]
    event_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AttemptEventKind):
            raise TypeError("typed attempt event kind required")
        if type(self.thesis_id) is not ThesisId:
            raise TypeError("typed thesis ID required")
        if type(self.setup_id) is not SetupId or type(self.attempt_id) is not AttemptId:
            raise TypeError("typed setup and attempt IDs required")
        _text(self.proposal_assembly_id, "proposal_assembly_id")
        require_utc(self.event_at, "event_at")
        require_utc(self.available_at, "available_at")
        if self.event_at > self.available_at:
            raise ValueError("attempt event cannot be available before occurrence")
        _unique_text(self.source_event_ids, "source_event_ids")
        object.__setattr__(self, "event_id", "ate_" + canonical_sha256((
            self.kind, self.thesis_id, self.setup_id, self.attempt_id,
            self.proposal_assembly_id, self.event_at, self.available_at,
            self.source_event_ids,
        )).lower())


@dataclass(frozen=True, slots=True)
class AttemptAdmissionResult:
    status: AttemptAdmissionStatus
    reason: AttemptAdmissionReason | None
    classification: ReentryClassification | None
    admission_event: AttemptLedgerEvent | None
    decision_at: datetime
    result_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, AttemptAdmissionStatus):
            raise TypeError("typed attempt admission status required")
        require_utc(self.decision_at, "decision_at")
        if self.status is AttemptAdmissionStatus.ADMITTED:
            if (
                self.reason is not None
                or not isinstance(self.classification, ReentryClassification)
                or not isinstance(self.admission_event, AttemptLedgerEvent)
                or self.admission_event.kind is not AttemptEventKind.ADMITTED
            ):
                raise ValueError("admitted result requires classification and event")
        elif (
            not isinstance(self.reason, AttemptAdmissionReason)
            or self.classification is not None
            or self.admission_event is not None
        ):
            raise ValueError("non-admitted result requires only typed reason")
        object.__setattr__(self, "result_id", "adr_" + canonical_sha256((
            self.status, self.reason, self.classification,
            self.admission_event.event_id if self.admission_event else None,
        )).lower())


def _result(
    status: AttemptAdmissionStatus,
    reason: AttemptAdmissionReason | None,
    decision_at: datetime,
    *,
    classification: ReentryClassification | None = None,
    event: AttemptLedgerEvent | None = None,
) -> AttemptAdmissionResult:
    return AttemptAdmissionResult(
        status, reason, classification, event, decision_at,
    )


def _history_index(
    history: tuple[AttemptLedgerEvent, ...],
) -> dict[AttemptId, tuple[AttemptLedgerEvent, AttemptLedgerEvent | None]] | None:
    if (
        not isinstance(history, tuple)
        or any(type(event) is not AttemptLedgerEvent for event in history)
        or len({event.event_id for event in history}) != len(history)
    ):
        return None
    index: dict[AttemptId, list[AttemptLedgerEvent | None]] = {}
    previous_available: datetime | None = None
    for event in history:
        if event.execution_authority:
            return None
        if previous_available is not None and event.available_at < previous_available:
            return None
        previous_available = event.available_at
        state = index.setdefault(event.attempt_id, [None, None])
        if event.kind is AttemptEventKind.ADMITTED:
            if state[0] is not None:
                return None
            state[0] = event
        else:
            admission = state[0]
            if (
                admission is None
                or state[1] is not None
                or event.event_at < admission.event_at
                or event.available_at < admission.available_at
                or event.thesis_id != admission.thesis_id
                or event.setup_id != admission.setup_id
                or event.proposal_assembly_id != admission.proposal_assembly_id
            ):
                return None
            state[1] = event
    if any(state[0] is None for state in index.values()):
        return None
    return {
        attempt_id: (state[0], state[1])  # type: ignore[arg-type]
        for attempt_id, state in index.items()
    }


def admit_attempt(
    *,
    proposal: AtomicResearchProposal,
    history: tuple[AttemptLedgerEvent, ...],
    reset: ResetEvidence | None,
    decision_at: datetime,
) -> AttemptAdmissionResult:
    """Admit one stable attempt or fail closed without changing supplied history."""

    require_utc(decision_at, "decision_at")
    if not isinstance(proposal, AtomicResearchProposal):
        raise TypeError("typed atomic research proposal required")
    if reset is not None and not isinstance(reset, ResetEvidence):
        raise TypeError("reset must be typed reset evidence or None")
    if proposal.execution_authority or proposal.decision.execution_authority:
        return _result(
            AttemptAdmissionStatus.UNRESOLVED,
            AttemptAdmissionReason.EXECUTION_AUTHORITY_PRESENT,
            decision_at,
        )
    if proposal.decision.decision_at != decision_at:
        return _result(
            AttemptAdmissionStatus.UNRESOLVED,
            AttemptAdmissionReason.DECISION_TIME_MISMATCH,
            decision_at,
        )
    index = _history_index(history)
    if index is None:
        return _result(
            AttemptAdmissionStatus.UNRESOLVED,
            AttemptAdmissionReason.HISTORY_INVALID,
            decision_at,
        )

    decision = proposal.decision
    if decision.attempt_id in index:
        return _result(
            AttemptAdmissionStatus.BLOCKED,
            AttemptAdmissionReason.DUPLICATE_ATTEMPT,
            decision_at,
        )
    same_thesis = sorted(
        (
            state for state in index.values()
            if state[0].thesis_id == decision.thesis_id
        ),
        key=lambda state: (state[0].event_at, state[0].event_id),
    )
    classification = ReentryClassification.NEW_THESIS
    sources = list(decision.input_evidence_ids)
    if same_thesis:
        if any(prior_close is None for _, prior_close in same_thesis):
            return _result(
                AttemptAdmissionStatus.BLOCKED,
                AttemptAdmissionReason.PRIOR_ATTEMPT_OPEN,
                decision_at,
            )
        prior_admission, prior_close = same_thesis[-1]
        if reset is not None and reset.available_at > decision_at:
            return _result(
                AttemptAdmissionStatus.UNRESOLVED,
                AttemptAdmissionReason.RESET_EVIDENCE_FUTURE,
                decision_at,
            )
        classification = classify_reentry(
            prior_thesis_id=prior_admission.thesis_id,
            candidate_thesis_id=decision.thesis_id,
            prior_close_at=prior_close.event_at,
            decision_at=decision_at,
            reset=reset,
        )
        if classification is ReentryClassification.SAME_THESIS_NO_RESET:
            return _result(
                AttemptAdmissionStatus.BLOCKED,
                AttemptAdmissionReason.SAME_THESIS_RESET_MISSING,
                decision_at,
            )
        if classification is ReentryClassification.CONTEXT_INVALIDATED:
            return _result(
                AttemptAdmissionStatus.BLOCKED,
                AttemptAdmissionReason.CONTEXT_INVALIDATED,
                decision_at,
            )
        sources.extend((prior_close.event_id, reset.reset_id))

    event = AttemptLedgerEvent(
        AttemptEventKind.ADMITTED,
        decision.thesis_id,
        decision.setup_id,
        decision.attempt_id,
        proposal.assembly_id,
        decision_at,
        decision_at,
        tuple(dict.fromkeys(sources)),
    )
    return _result(
        AttemptAdmissionStatus.ADMITTED,
        None,
        decision_at,
        classification=classification,
        event=event,
    )


def record_attempt_close(
    *,
    admission: AttemptLedgerEvent,
    history: tuple[AttemptLedgerEvent, ...],
    closed_at: datetime,
    available_at: datetime,
    source_event_ids: tuple[str, ...],
) -> AttemptLedgerEvent:
    """Build a close event only for an admitted, still-open attempt."""

    if not isinstance(admission, AttemptLedgerEvent):
        raise TypeError("typed attempt admission required")
    if admission.kind is not AttemptEventKind.ADMITTED:
        raise ValueError("only an admission can be closed")
    index = _history_index(history)
    state = index.get(admission.attempt_id) if index is not None else None
    if (
        state is None
        or state[0].event_id != admission.event_id
        or state[1] is not None
    ):
        raise ValueError("attempt history does not contain one open admission")
    require_utc(closed_at, "closed_at")
    require_utc(available_at, "available_at")
    if closed_at < admission.event_at or available_at < admission.available_at:
        raise ValueError("attempt close cannot predate admission")
    return AttemptLedgerEvent(
        AttemptEventKind.CLOSED,
        admission.thesis_id,
        admission.setup_id,
        admission.attempt_id,
        admission.proposal_assembly_id,
        closed_at,
        available_at,
        source_event_ids,
    )
