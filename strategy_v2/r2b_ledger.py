"""Pure R2B finality transitions; persistence remains an external adapter concern."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .causality import require_utc
from .events import canonical_sha256
from .r2b_models import R2BOutcome, R2BStructuralEvaluation


class SlotClaimStatus(str, Enum):
    CLAIMED = "CLAIMED"
    IDEMPOTENT = "IDEMPOTENT"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    UNRESOLVED_IGNORED = "UNRESOLVED_IGNORED"


@dataclass(frozen=True, slots=True)
class MarketSlotClaim:
    market_slot_key: str
    market_event_id: str
    outcome: R2BOutcome
    source_hash: str
    claimed_at: datetime
    claim_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("market_slot_key", "market_event_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} required")
        if self.outcome not in (
            R2BOutcome.INITIAL_BREAK_CONFIRMED,
            R2BOutcome.CONTINUATION_BOS_CONFIRMED,
            R2BOutcome.CHOCH_CONFIRMED,
            R2BOutcome.FINAL_NO_CROSS,
        ):
            raise ValueError("only a final market outcome may claim a slot")
        if len(self.source_hash) != 64:
            raise ValueError("source_hash must be SHA-256")
        require_utc(self.claimed_at, "claimed_at")
        object.__setattr__(self, "claim_id", "clm_" + canonical_sha256((
            self.market_slot_key, self.market_event_id, self.outcome,
            self.source_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class SlotClaimTransition:
    status: SlotClaimStatus
    claims: tuple[MarketSlotClaim, ...]
    accepted_claim_id: str | None
    conflicting_source_hashes: tuple[str, ...]
    transition_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, SlotClaimStatus):
            raise TypeError("typed claim status required")
        if len({claim.market_slot_key for claim in self.claims}) != len(self.claims):
            raise ValueError("at most one accepted claim per market slot")
        if self.status is SlotClaimStatus.SOURCE_CONFLICT:
            if len(self.conflicting_source_hashes) != 2:
                raise ValueError("source conflict must preserve both submitted hashes")
        elif self.conflicting_source_hashes:
            raise ValueError("non-conflict transition cannot carry conflict hashes")
        object.__setattr__(self, "transition_id", "trn_" + canonical_sha256((
            self.status, self.claims, self.accepted_claim_id,
            self.conflicting_source_hashes,
        )).lower())


def claim_market_slot(
    existing: tuple[MarketSlotClaim, ...],
    evaluation: R2BStructuralEvaluation,
    *,
    claimed_at: datetime,
) -> SlotClaimTransition:
    """Model one atomic uniqueness decision without mutating or overwriting input."""
    require_utc(claimed_at, "claimed_at")
    if not isinstance(existing, tuple) or any(
        not isinstance(item, MarketSlotClaim) for item in existing
    ):
        raise TypeError("existing claims must be typed and immutable")
    if not isinstance(evaluation, R2BStructuralEvaluation):
        raise TypeError("typed R2B evaluation required")
    if evaluation.market_event_id is None or evaluation.slot is None:
        return SlotClaimTransition(
            SlotClaimStatus.UNRESOLVED_IGNORED, existing, None, (),
        )
    proposed = MarketSlotClaim(
        evaluation.slot.market_slot_key,
        evaluation.market_event_id,
        evaluation.outcome,
        evaluation.source_hash,
        claimed_at,
    )
    prior = next((item for item in existing
                  if item.market_slot_key == proposed.market_slot_key), None)
    if prior is None:
        return SlotClaimTransition(
            SlotClaimStatus.CLAIMED, existing + (proposed,), proposed.claim_id, (),
        )
    if prior.claim_id == proposed.claim_id:
        return SlotClaimTransition(
            SlotClaimStatus.IDEMPOTENT, existing, prior.claim_id, (),
        )
    return SlotClaimTransition(
        SlotClaimStatus.SOURCE_CONFLICT,
        existing,
        None,
        (prior.source_hash, proposed.source_hash),
    )
