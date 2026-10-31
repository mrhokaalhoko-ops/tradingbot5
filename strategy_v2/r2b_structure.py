"""Pure R2B role, first-cross, and protection-candidate evaluation."""

from __future__ import annotations

from datetime import datetime

from .causality import require_utc
from .enums import Side
from .events import canonical_sha256
from .market_data import ClosedBar, validate_bar_series
from .r2a_models import CloseBreakCandidate, ThresholdStatus
from .r2b_models import (
    BarIneligibilityDisposition,
    ContinuationRoleResolution,
    ContinuationStructuralRole,
    FrozenStructuralEpoch,
    InitialRoleResolution,
    InitialStructuralRole,
    MarketSlotIdentity,
    ProtectionCandidateAssessment,
    ProtectionCandidateLedger,
    ProtectedCrossResolution,
    R2BEvaluationFamily,
    R2BOutcome,
    R2BReason,
    R2BStructuralEvaluation,
    R2BVariant,
    SimulatedResearchGuard,
)
from .structure_models import BreakDirection, ConfirmedPivot, PivotType


class R2BContractError(ValueError):
    """The caller omitted the decision-time anchor required by every result."""


def _complete_bar_path(bars: tuple[ClosedBar, ...], decision_at: datetime) -> bool:
    try:
        validate_bar_series(bars, as_of=decision_at)
    except (TypeError, ValueError):
        return False
    return bool(bars) and all(
        current.open_time == previous.close_time
        for previous, current in zip(bars, bars[1:])
    )


def _context_matches(
    bars: tuple[ClosedBar, ...],
    pivots: tuple[ConfirmedPivot, ...],
    epoch: FrozenStructuralEpoch,
    variant: R2BVariant,
) -> bool:
    expected = (variant.symbol, variant.timeframe)
    return (
        (epoch.symbol, epoch.timeframe) == expected
        and all((bar.symbol, bar.timeframe) == expected for bar in bars)
        and all((pivot.symbol, pivot.timeframe) == expected for pivot in pivots)
        and epoch.variant_hash == variant.variant_hash
    )


def _epoch_dataset_matches(bars: tuple[ClosedBar, ...], epoch: FrozenStructuralEpoch) -> bool:
    return canonical_sha256(bars) == epoch.dataset_hash


def _pivots_match_source_bars(
    pivots: tuple[ConfirmedPivot, ...], bars: tuple[ClosedBar, ...]
) -> bool:
    by_id = {bar.bar_id: bar for bar in bars}
    for pivot in pivots:
        try:
            support = tuple(by_id[item_id] for item_id in pivot.parent_evidence_ids)
            source = by_id[pivot.source_bar_id]
            confirmation = by_id[pivot.confirmation_bar_id]
        except KeyError:
            return False
        if (canonical_sha256(support) != pivot.source_window_hash
                or pivot.source_at != source.close_time
                or pivot.confirmed_at != confirmation.close_time
                or pivot.available_at != confirmation.available_at):
            return False
    return True


def _role_failure(reason: R2BReason, evidence_ids: tuple[str, ...]) -> InitialRoleResolution:
    return InitialRoleResolution(None, reason, tuple(dict.fromkeys(evidence_ids)))


def resolve_initial_role(
    *,
    pivots: tuple[ConfirmedPivot, ...],
    bars: tuple[ClosedBar, ...],
    candidate_bar_id: str,
    side: Side,
    epoch: FrozenStructuralEpoch,
    variant: R2BVariant,
    decision_at: datetime,
) -> InitialRoleResolution:
    """Resolve the terminal consecutive four-pivot role known before candidate open."""
    try:
        require_utc(decision_at, "decision_at")
    except ValueError as exc:
        raise R2BContractError("decision_at must be an aware UTC timestamp") from exc
    if (not isinstance(pivots, tuple) or any(type(p) is not ConfirmedPivot for p in pivots)
            or not isinstance(bars, tuple) or any(type(b) is not ClosedBar for b in bars)
            or not isinstance(side, Side) or not isinstance(epoch, FrozenStructuralEpoch)
            or not isinstance(variant, R2BVariant)):
        return _role_failure(R2BReason.INVALID_INPUT_TYPE, ())
    evidence = tuple(p.pivot_id for p in pivots) + tuple(b.bar_id for b in bars)
    candidate = next((bar for bar in bars if bar.bar_id == candidate_bar_id), None)
    if candidate is None:
        return _role_failure(R2BReason.CANDIDATE_BAR_NOT_FOUND, evidence)
    if not epoch.complete_prefix:
        return _role_failure(R2BReason.EPOCH_INCOMPLETE, evidence)
    candidate_index = bars.index(candidate)
    observed_bars = bars[:candidate_index + 1]
    if (not _epoch_dataset_matches(bars, epoch)
            or not _complete_bar_path(observed_bars, decision_at)
            or not _context_matches(observed_bars, pivots, epoch, variant)):
        return _role_failure(R2BReason.MIXED_CONTEXT, evidence)
    if (bars[0].open_time < epoch.start_at or candidate.close_time > epoch.end_at or
            epoch.witness_available_at > decision_at):
        return _role_failure(R2BReason.FUTURE_EVIDENCE, evidence)
    eligible = tuple(sorted(
        (pivot for pivot in pivots
         if epoch.start_at <= pivot.source_at < candidate.open_time
         and pivot.available_at < candidate.open_time),
        key=lambda pivot: (pivot.source_at, pivot.pivot_id),
    ))
    if len(eligible) < 4:
        return _role_failure(R2BReason.STRUCTURE_ROLE_UNRESOLVED, evidence)
    if (any(p.available_at > decision_at for p in eligible) or
            any(left.source_at == right.source_at for left, right in zip(eligible, eligible[1:])) or
            any(left.pivot_type is right.pivot_type for left, right in zip(eligible, eligible[1:]))):
        return _role_failure(R2BReason.STRUCTURE_ROLE_UNRESOLVED, evidence)
    if not _pivots_match_source_bars(eligible, observed_bars):
        return _role_failure(R2BReason.STRUCTURE_ROLE_UNRESOLVED, evidence)
    role_pivots = eligible[-4:]
    expected = ((PivotType.HIGH, PivotType.LOW, PivotType.HIGH, PivotType.LOW)
                if side is Side.LONG else
                (PivotType.LOW, PivotType.HIGH, PivotType.LOW, PivotType.HIGH))
    if tuple(p.pivot_type for p in role_pivots) != expected:
        return _role_failure(R2BReason.ROLE_PATTERN_INVALID, evidence)
    old_reference, old_context, reference, context = role_pivots
    directional = (reference.price > old_reference.price and context.price > old_context.price
                   if side is Side.LONG else
                   reference.price < old_reference.price and context.price < old_context.price)
    if not directional:
        return _role_failure(R2BReason.ROLE_PATTERN_INVALID, evidence)
    same_type = tuple(p for p in eligible if p.pivot_type is reference.pivot_type)
    extreme = (max(p.price for p in same_type) if side is Side.LONG
               else min(p.price for p in same_type))
    if reference.price != extreme or sum(p.price == extreme for p in same_type) != 1:
        return _role_failure(R2BReason.REFERENCE_NOT_UNIQUE_EXTREME, evidence)
    role_known = max(epoch.witness_available_at, *(p.available_at for p in role_pivots))
    first_eligible = next((bar for bar in bars if bar.open_time > role_known), None)
    if first_eligible is None or candidate.open_time < first_eligible.open_time:
        return _role_failure(R2BReason.ROLE_NOT_YET_KNOWABLE, evidence)
    role = InitialStructuralRole(
        side=side,
        pivot_ids=tuple(p.pivot_id for p in role_pivots),
        reference_pivot_id=reference.pivot_id,
        contextual_pivot_id=context.pivot_id,
        role_first_knowable_at=role_known,
        first_eligible_bar_id=first_eligible.bar_id,
        first_eligible_bar_open_at=first_eligible.open_time,
        epoch_id=epoch.epoch_id,
        variant_hash=variant.variant_hash,
    )
    return InitialRoleResolution(role, None, tuple(p.pivot_id for p in role_pivots))


def _r2a_semantic_snapshot(candidate: CloseBreakCandidate) -> tuple[object, ...]:
    """Stable market semantics; deliberately excludes decision_at and R2A event_id."""
    return (
        candidate.status, candidate.reason, candidate.direction, candidate.symbol,
        candidate.timeframe, candidate.previous_bar_id, candidate.break_bar_id,
        candidate.broken_pivot_id, candidate.atr_evidence_id,
        candidate.tick_evidence_id, candidate.threshold_delta,
        candidate.threshold_price, candidate.pivot_source_window_hash,
        candidate.break_support_hash, candidate.variant_hash,
        candidate.threshold_policy_id, candidate.rule_version,
        candidate.source_commit, candidate.atr_source_policy,
        candidate.atr_method_id, candidate.atr_period, candidate.atr_policy_id,
        candidate.price_basis, candidate.source_at, candidate.confirmed_at,
        candidate.available_at, candidate.parent_evidence_ids,
    )


def _numeric_observation_matches_bars(
    candidate: CloseBreakCandidate, previous: ClosedBar, current: ClosedBar
) -> bool:
    if candidate.threshold_price is None:
        return False
    crossed = (
        previous.close <= candidate.threshold_price < current.close
        if candidate.direction is BreakDirection.UP else
        previous.close >= candidate.threshold_price > current.close
    )
    expected = (ThresholdStatus.THRESHOLD_PASSED if crossed
                else ThresholdStatus.THRESHOLD_NOT_PASSED)
    return candidate.status is expected


def _slot(
    variant: R2BVariant,
    epoch: FrozenStructuralEpoch,
    role: InitialStructuralRole,
    bar_id: str,
) -> MarketSlotIdentity:
    return MarketSlotIdentity(
        R2BEvaluationFamily.INITIAL_BREAK,
        variant.symbol,
        variant.timeframe,
        epoch.epoch_id,
        role.reference_pivot_id,
        bar_id,
        role.side,
        variant.verified_feed_basis_id,
        variant.variant_hash,
    )


def _evaluation(
    *,
    outcome: R2BOutcome,
    reason: R2BReason | None,
    slot: MarketSlotIdentity | None,
    role: InitialStructuralRole | ContinuationStructuralRole | None,
    evidence_ids: tuple[str, ...],
    source: object,
    decision_at: datetime,
) -> R2BStructuralEvaluation:
    return R2BStructuralEvaluation(
        outcome=outcome,
        reason=reason,
        slot=slot,
        role_id=role.role_id if role else None,
        parent_evidence_ids=tuple(dict.fromkeys(evidence_ids)),
        source_hash=canonical_sha256(source),
        decision_at=decision_at,
    )


def _continuation_failure(
    reason: R2BReason, evidence_ids: tuple[str, ...]
) -> ContinuationRoleResolution:
    return ContinuationRoleResolution(None, reason, tuple(dict.fromkeys(evidence_ids)))


def resolve_continuation_role(
    *,
    initial_evaluation: R2BStructuralEvaluation,
    initial_reference_pivot: ConfirmedPivot,
    initial_context_pivot: ConfirmedPivot,
    pivots: tuple[ConfirmedPivot, ...],
    bars: tuple[ClosedBar, ...],
    candidate_bar_id: str,
    side: Side,
    epoch: FrozenStructuralEpoch,
    variant: R2BVariant,
    decision_at: datetime,
) -> ContinuationRoleResolution:
    """Freeze one uncontested post-initial reference and all eligible retraces."""
    require_utc(decision_at, "decision_at")
    if (not isinstance(initial_evaluation, R2BStructuralEvaluation)
            or type(initial_reference_pivot) is not ConfirmedPivot
            or type(initial_context_pivot) is not ConfirmedPivot
            or not isinstance(pivots, tuple)
            or any(type(p) is not ConfirmedPivot for p in pivots)
            or not isinstance(bars, tuple) or any(type(b) is not ClosedBar for b in bars)
            or not isinstance(side, Side)
            or not isinstance(epoch, FrozenStructuralEpoch)
            or not isinstance(variant, R2BVariant)):
        return _continuation_failure(R2BReason.INVALID_INPUT_TYPE, ())
    evidence = tuple(p.pivot_id for p in pivots)
    if (initial_evaluation.outcome is not R2BOutcome.INITIAL_BREAK_CONFIRMED
            or initial_evaluation.market_event_id is None
            or initial_evaluation.slot is None):
        return _continuation_failure(R2BReason.INITIAL_EVENT_NOT_CONFIRMED, evidence)
    initial_bar = next((bar for bar in bars
                        if bar.bar_id == initial_evaluation.slot.bar_id), None)
    candidate = next((bar for bar in bars if bar.bar_id == candidate_bar_id), None)
    if initial_bar is None or candidate is None:
        return _continuation_failure(R2BReason.CANDIDATE_BAR_NOT_FOUND, evidence)
    observed = bars[:bars.index(candidate) + 1]
    if (not _epoch_dataset_matches(bars, epoch)
            or not _complete_bar_path(observed, decision_at)
            or not _context_matches(observed, pivots, epoch, variant)):
        return _continuation_failure(R2BReason.MIXED_CONTEXT, evidence)
    reference_type = PivotType.HIGH if side is Side.LONG else PivotType.LOW
    references = tuple(sorted((
        pivot for pivot in pivots
        if pivot.pivot_type is reference_type
        and initial_bar.close_time < pivot.source_at < candidate.open_time
        and pivot.available_at < candidate.open_time
        and (pivot.price > initial_reference_pivot.price if side is Side.LONG
             else pivot.price < initial_reference_pivot.price)
    ), key=lambda pivot: (pivot.source_at, pivot.pivot_id)))
    if not references:
        return _continuation_failure(R2BReason.NO_CONTINUATION_REFERENCE, evidence)
    if len(references) != 1:
        return _continuation_failure(R2BReason.COMPETING_REFERENCE, evidence)
    reference = references[0]
    retrace_type = PivotType.LOW if side is Side.LONG else PivotType.HIGH
    retraces = tuple(sorted((
        pivot for pivot in pivots
        if pivot.pivot_type is retrace_type
        and reference.source_at < pivot.source_at < candidate.open_time
        and pivot.available_at < candidate.open_time
    ), key=lambda pivot: (pivot.source_at, pivot.pivot_id)))
    if not retraces:
        return _continuation_failure(R2BReason.NO_CONTINUATION_RETRACE, evidence)
    if not _pivots_match_source_bars((initial_reference_pivot, initial_context_pivot,
                                      reference, *retraces), observed):
        return _continuation_failure(R2BReason.STRUCTURE_ROLE_UNRESOLVED, evidence)
    role_known = max(epoch.witness_available_at, initial_bar.available_at,
                     reference.available_at, min(q.available_at for q in retraces))
    first_eligible = next((bar for bar in bars if bar.open_time > role_known), None)
    if first_eligible is None or candidate.open_time < first_eligible.open_time:
        return _continuation_failure(R2BReason.ROLE_NOT_YET_KNOWABLE, evidence)
    role = ContinuationStructuralRole(
        side=side,
        initial_event_id=initial_evaluation.market_event_id,
        initial_break_bar_id=initial_bar.bar_id,
        initial_reference_pivot_id=initial_reference_pivot.pivot_id,
        counter_reference_pivot_id=initial_context_pivot.pivot_id,
        reference_pivot_id=reference.pivot_id,
        retrace_pivot_ids=tuple(q.pivot_id for q in retraces),
        role_first_knowable_at=role_known,
        first_eligible_bar_id=first_eligible.bar_id,
        first_eligible_bar_open_at=first_eligible.open_time,
        epoch_id=epoch.epoch_id,
        variant_hash=variant.variant_hash,
    )
    return ContinuationRoleResolution(
        role, None,
        (initial_evaluation.market_event_id, initial_context_pivot.pivot_id,
         reference.pivot_id,
         *(q.pivot_id for q in retraces)),
    )


def evaluate_continuation_bos(
    *,
    role_resolution: ContinuationRoleResolution,
    pivots: tuple[ConfirmedPivot, ...],
    bars: tuple[ClosedBar, ...],
    numeric_candidates: tuple[CloseBreakCandidate, ...],
    counter_candidates: tuple[CloseBreakCandidate, ...],
    counter_coverage_bar_ids: tuple[str, ...],
    candidate_bar_id: str,
    epoch: FrozenStructuralEpoch,
    variant: R2BVariant,
    decision_at: datetime,
) -> R2BStructuralEvaluation:
    """Confirm only the first uncontested continuation cross after I→P→Q."""
    require_utc(decision_at, "decision_at")
    if not isinstance(role_resolution, ContinuationRoleResolution):
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED, reason=R2BReason.INVALID_INPUT_TYPE,
            slot=None, role=None, evidence_ids=(), source=("invalid-role",),
            decision_at=decision_at,
        )
    role = role_resolution.role
    if role is None:
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED, reason=role_resolution.reason,
            slot=None, role=None, evidence_ids=role_resolution.evidence_ids,
            source=role_resolution, decision_at=decision_at,
        )
    candidate = next((bar for bar in bars if bar.bar_id == candidate_bar_id), None)
    reference = next((p for p in pivots if p.pivot_id == role.reference_pivot_id), None)
    if candidate is None or reference is None:
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=R2BReason.CANDIDATE_BAR_NOT_FOUND,
            slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
            source=(role, candidate_bar_id), decision_at=decision_at,
        )
    candidate_index = bars.index(candidate)
    observed = bars[:candidate_index + 1]
    if (not _epoch_dataset_matches(bars, epoch)
            or not _complete_bar_path(observed, decision_at)):
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=R2BReason.INCOMPLETE_FIRST_CROSS_HISTORY,
            slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
            source=(role, observed), decision_at=decision_at,
        )
    initial_bar = next((bar for bar in bars if bar.bar_id == role.initial_break_bar_id), None)
    expected_counter_bars = tuple(
        bar for bar in bars
        if initial_bar is not None and initial_bar.close_time < bar.close_time < candidate.open_time
    )
    counter_by_bar = {item.break_bar_id: item for item in counter_candidates}
    expected_counter_ids = tuple(bar.bar_id for bar in expected_counter_bars)
    counter_direction = BreakDirection.DOWN if role.side is Side.LONG else BreakDirection.UP
    counter_valid = (
        initial_bar is not None
        and isinstance(counter_coverage_bar_ids, tuple)
        and counter_coverage_bar_ids == expected_counter_ids
        and len(counter_by_bar) == len(counter_candidates) == len(expected_counter_bars)
        and all(
            (item := counter_by_bar.get(bar.bar_id)) is not None
            and item.status is not ThresholdStatus.UNRESOLVED
            and item.variant_hash == variant.r2a_variant_hash
            and item.broken_pivot_id == role.counter_reference_pivot_id
            and item.direction is counter_direction
            and item.previous_bar_id == bars[bars.index(bar) - 1].bar_id
            and item.available_at is not None
            and item.available_at <= decision_at
            and _numeric_observation_matches_bars(item, bars[bars.index(bar) - 1], bar)
            for bar in expected_counter_bars
        )
    )
    if not counter_valid:
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=R2BReason.INCOMPLETE_COUNTER_EVENT_HISTORY,
            slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
            source=(role, tuple(_r2a_semantic_snapshot(c) for c in counter_candidates)),
            decision_at=decision_at,
        )
    if any(item.status is ThresholdStatus.THRESHOLD_PASSED for item in counter_candidates):
        return _evaluation(
            outcome=R2BOutcome.DISQUALIFIED, reason=R2BReason.COUNTER_EVENT,
            slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
            source=(role, tuple(_r2a_semantic_snapshot(c) for c in counter_candidates)),
            decision_at=decision_at,
        )
    first_index = next((i for i, bar in enumerate(bars)
                        if bar.bar_id == role.first_eligible_bar_id), None)
    if first_index is None or candidate_index < first_index:
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED, reason=R2BReason.ROLE_NOT_YET_KNOWABLE,
            slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
            source=(role, bars), decision_at=decision_at,
        )
    prior_window = tuple(bar for bar in bars
                         if reference.source_at < bar.close_time < role.first_eligible_bar_open_at)
    if any(bar.close > reference.price if role.side is Side.LONG
           else bar.close < reference.price for bar in prior_window):
        return _evaluation(
            outcome=R2BOutcome.DISQUALIFIED,
            reason=R2BReason.REFERENCE_PREVIOUSLY_BROKEN,
            slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
            source=(role, prior_window), decision_at=decision_at,
        )
    by_bar = {item.break_bar_id: item for item in numeric_candidates}
    if len(by_bar) != len(numeric_candidates):
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED, reason=R2BReason.SOURCE_CONFLICT,
            slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
            source=(role, tuple(_r2a_semantic_snapshot(c) for c in numeric_candidates)),
            decision_at=decision_at,
        )
    direction = BreakDirection.UP if role.side is Side.LONG else BreakDirection.DOWN
    ordered: list[CloseBreakCandidate] = []
    for index in range(first_index, candidate_index + 1):
        bar = bars[index]
        item = by_bar.get(bar.bar_id)
        if (item is None or item.status is ThresholdStatus.UNRESOLVED
                or item.variant_hash != variant.r2a_variant_hash
                or item.broken_pivot_id != reference.pivot_id
                or item.direction is not direction
                or item.previous_bar_id != bars[index - 1].bar_id
                or item.source_at != bar.close_time
                or not _numeric_observation_matches_bars(item, bars[index - 1], bar)
                or item.available_at is None or item.available_at > decision_at):
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.INCOMPLETE_FIRST_CROSS_HISTORY,
                slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
                source=(role, tuple(_r2a_semantic_snapshot(c) for c in numeric_candidates)),
                decision_at=decision_at,
            )
        ordered.append(item)
    if any(item.status is ThresholdStatus.THRESHOLD_PASSED for item in ordered[:-1]):
        return _evaluation(
            outcome=R2BOutcome.DISQUALIFIED,
            reason=R2BReason.REPEATED_CONTINUATION_CROSS,
            slot=None, role=role, evidence_ids=role_resolution.evidence_ids,
            source=(role, tuple(_r2a_semantic_snapshot(c) for c in ordered)),
            decision_at=decision_at,
        )
    slot = MarketSlotIdentity(
        R2BEvaluationFamily.CONTINUATION_BOS, variant.symbol, variant.timeframe,
        epoch.epoch_id, reference.pivot_id, candidate.bar_id, role.side,
        variant.verified_feed_basis_id, variant.variant_hash,
    )
    outcome = (R2BOutcome.CONTINUATION_BOS_CONFIRMED
               if ordered[-1].status is ThresholdStatus.THRESHOLD_PASSED
               else R2BOutcome.FINAL_NO_CROSS)
    evidence_ids = role_resolution.evidence_ids + tuple(
        evidence_id for item in ordered for evidence_id in item.parent_evidence_ids
    )
    return _evaluation(
        outcome=outcome, reason=None, slot=slot, role=role,
        evidence_ids=evidence_ids,
        source=(role, tuple(_r2a_semantic_snapshot(c) for c in ordered)),
        decision_at=decision_at,
    )


def evaluate_protected_close_cross(
    *,
    guard: SimulatedResearchGuard,
    bars: tuple[ClosedBar, ...],
    numeric_candidates: tuple[CloseBreakCandidate, ...],
    candidate_bar_id: str,
    epoch: FrozenStructuralEpoch,
    variant: R2BVariant,
    decision_at: datetime,
) -> ProtectedCrossResolution:
    """Evaluate the first post-activation protected close cross without thesis effects."""
    require_utc(decision_at, "decision_at")
    candidate = next((bar for bar in bars if bar.bar_id == candidate_bar_id), None)
    if candidate is None:
        evaluation = _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=R2BReason.CANDIDATE_BAR_NOT_FOUND,
            slot=None, role=None, evidence_ids=(guard.guard_id,),
            source=(guard, candidate_bar_id), decision_at=decision_at,
        )
        return ProtectedCrossResolution(evaluation, None, False)
    index = bars.index(candidate)
    observed = bars[:index + 1]
    if (not _epoch_dataset_matches(bars, epoch)
            or not _complete_bar_path(observed, decision_at)
            or (guard.symbol, guard.timeframe, guard.epoch_id, guard.variant_hash) !=
            (variant.symbol, variant.timeframe, epoch.epoch_id, variant.variant_hash)):
        evaluation = _evaluation(
            outcome=R2BOutcome.UNRESOLVED, reason=R2BReason.MIXED_CONTEXT,
            slot=None, role=None, evidence_ids=(guard.guard_id,),
            source=(guard, observed), decision_at=decision_at,
        )
        return ProtectedCrossResolution(evaluation, None, False)
    previous = bars[index - 1] if index else None
    if previous is None or previous.open_time <= guard.market_active_from:
        disposition = BarIneligibilityDisposition(
            guard.guard_id, guard.protection_epoch_id, guard.symbol,
            guard.timeframe, candidate.bar_id, variant.variant_hash,
            R2BReason.NO_POST_ACTIVATION_PREVIOUS_CLOSE,
        )
        evaluation = _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=R2BReason.NO_POST_ACTIVATION_PREVIOUS_CLOSE,
            slot=None, role=None,
            evidence_ids=(guard.guard_id, previous.bar_id if previous else candidate.bar_id),
            source=(guard, previous, candidate, disposition), decision_at=decision_at,
        )
        return ProtectedCrossResolution(evaluation, disposition, False)
    eligible_indices = tuple(i for i, bar in enumerate(bars[:index + 1])
                             if i > 0 and bars[i - 1].open_time > guard.market_active_from)
    by_bar = {item.break_bar_id: item for item in numeric_candidates}
    direction = BreakDirection.DOWN if guard.side is Side.LONG else BreakDirection.UP
    ordered: list[CloseBreakCandidate] = []
    for eligible_index in eligible_indices:
        bar = bars[eligible_index]
        item = by_bar.get(bar.bar_id)
        if (item is None or item.status is ThresholdStatus.UNRESOLVED
                or item.broken_pivot_id != guard.level_pivot_id
                or item.direction is not direction
                or item.variant_hash != variant.r2a_variant_hash
                or item.previous_bar_id != bars[eligible_index - 1].bar_id
                or not _numeric_observation_matches_bars(
                    item, bars[eligible_index - 1], bar
                )
                or item.available_at is None or item.available_at > decision_at):
            evaluation = _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.INCOMPLETE_FIRST_BREACH_HISTORY,
                slot=None, role=None, evidence_ids=(guard.guard_id,),
                source=(guard, tuple(_r2a_semantic_snapshot(c) for c in numeric_candidates)),
                decision_at=decision_at,
            )
            return ProtectedCrossResolution(evaluation, None, False)
        ordered.append(item)
    if any(item.status is ThresholdStatus.THRESHOLD_PASSED for item in ordered[:-1]):
        evaluation = _evaluation(
            outcome=R2BOutcome.DISQUALIFIED,
            reason=R2BReason.GUARD_BREACH_PENDING_DISPOSITION,
            slot=None, role=None, evidence_ids=(guard.guard_id,),
            source=(guard, tuple(_r2a_semantic_snapshot(c) for c in ordered)),
            decision_at=decision_at,
        )
        return ProtectedCrossResolution(evaluation, None, True)
    slot = MarketSlotIdentity(
        R2BEvaluationFamily.PROTECTED_CLOSE_CROSS,
        variant.symbol, variant.timeframe, epoch.epoch_id,
        guard.level_pivot_id, candidate.bar_id, guard.side,
        variant.verified_feed_basis_id, variant.variant_hash,
        guard.guard_id, guard.protection_epoch_id,
    )
    outcome = (R2BOutcome.CHOCH_CONFIRMED
               if ordered and ordered[-1].status is ThresholdStatus.THRESHOLD_PASSED
               else R2BOutcome.FINAL_NO_CROSS)
    evidence_ids = (guard.guard_id,) + tuple(
        evidence_id for item in ordered for evidence_id in item.parent_evidence_ids
    )
    evaluation = _evaluation(
        outcome=outcome, reason=None, slot=slot, role=None,
        evidence_ids=evidence_ids,
        source=(guard, tuple(_r2a_semantic_snapshot(c) for c in ordered)),
        decision_at=decision_at,
    )
    return ProtectedCrossResolution(
        evaluation, None, outcome is R2BOutcome.CHOCH_CONFIRMED,
    )


def evaluate_initial_break(
    *,
    role_resolution: InitialRoleResolution,
    pivots: tuple[ConfirmedPivot, ...],
    bars: tuple[ClosedBar, ...],
    numeric_candidates: tuple[CloseBreakCandidate, ...],
    candidate_bar_id: str,
    epoch: FrozenStructuralEpoch,
    variant: R2BVariant,
    decision_at: datetime,
) -> R2BStructuralEvaluation:
    """Promote one R2A observation only after full first-cross structural checks."""
    try:
        require_utc(decision_at, "decision_at")
    except ValueError as exc:
        raise R2BContractError("decision_at must be an aware UTC timestamp") from exc
    if (not isinstance(role_resolution, InitialRoleResolution)
            or not isinstance(pivots, tuple)
            or any(type(p) is not ConfirmedPivot for p in pivots)
            or not isinstance(bars, tuple) or any(type(b) is not ClosedBar for b in bars)
            or not isinstance(numeric_candidates, tuple)
            or any(type(c) is not CloseBreakCandidate for c in numeric_candidates)
            or not isinstance(epoch, FrozenStructuralEpoch)
            or not isinstance(variant, R2BVariant)):
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED, reason=R2BReason.INVALID_INPUT_TYPE,
            slot=None, role=None, evidence_ids=(), source=("invalid-input",),
            decision_at=decision_at,
        )
    role = role_resolution.role
    base_evidence = role_resolution.evidence_ids
    if role is None:
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=role_resolution.reason,
            slot=None,
            role=None,
            evidence_ids=base_evidence,
            source=role_resolution,
            decision_at=decision_at,
        )
    candidate_bar = next((bar for bar in bars if bar.bar_id == candidate_bar_id), None)
    if candidate_bar is None:
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=R2BReason.CANDIDATE_BAR_NOT_FOUND,
            slot=None, role=role, evidence_ids=base_evidence,
            source=(role, candidate_bar_id), decision_at=decision_at,
        )
    current_slot = _slot(variant, epoch, role, candidate_bar_id)
    candidates_by_bar = {candidate.break_bar_id: candidate for candidate in numeric_candidates}
    if len(candidates_by_bar) != len(numeric_candidates):
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED, reason=R2BReason.SOURCE_CONFLICT,
            slot=None, role=role, evidence_ids=base_evidence,
            source=(role, tuple(_r2a_semantic_snapshot(c) for c in numeric_candidates)),
            decision_at=decision_at,
        )
    reference = next((p for p in pivots if p.pivot_id == role.reference_pivot_id), None)
    context = next((p for p in pivots if p.pivot_id == role.contextual_pivot_id), None)
    candidate_index = bars.index(candidate_bar)
    observed_bars = bars[:candidate_index + 1]
    if reference is None or context is None or not _complete_bar_path(observed_bars, decision_at):
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=R2BReason.INCOMPLETE_FIRST_CROSS_HISTORY,
            slot=None, role=role, evidence_ids=base_evidence,
            source=(role, bars), decision_at=decision_at,
        )
    if not _context_matches(observed_bars, pivots, epoch, variant):
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED, reason=R2BReason.MIXED_CONTEXT,
            slot=None, role=role, evidence_ids=base_evidence,
            source=(role, bars, pivots), decision_at=decision_at,
        )
    first_index = next((index for index, bar in enumerate(bars)
                        if bar.bar_id == role.first_eligible_bar_id), None)
    candidate_index = next((index for index, bar in enumerate(bars)
                            if bar.bar_id == candidate_bar_id), None)
    if first_index is None or candidate_index is None or candidate_index < first_index:
        return _evaluation(
            outcome=R2BOutcome.UNRESOLVED,
            reason=R2BReason.ROLE_NOT_YET_KNOWABLE,
            slot=None, role=role, evidence_ids=base_evidence,
            source=(role, bars), decision_at=decision_at,
        )
    prior_window = tuple(
        bar for bar in bars
        if reference.source_at < bar.close_time < role.first_eligible_bar_open_at
    )
    prior_breach = any(
        bar.close > reference.price if role.side is Side.LONG
        else bar.close < reference.price
        for bar in prior_window
    )
    if prior_breach:
        return _evaluation(
            outcome=R2BOutcome.DISQUALIFIED,
            reason=R2BReason.REFERENCE_PREVIOUSLY_BROKEN,
            slot=None, role=role, evidence_ids=base_evidence + tuple(b.bar_id for b in prior_window),
            source=(role, prior_window), decision_at=decision_at,
        )
    context_window = tuple(
        bar for bar in bars
        if context.confirmed_at <= bar.close_time <= candidate_bar.close_time
    )
    context_breach = any(
        bar.close < context.price if role.side is Side.LONG
        else bar.close > context.price
        for bar in context_window
    )
    if context_breach:
        return _evaluation(
            outcome=R2BOutcome.DISQUALIFIED,
            reason=R2BReason.CONTEXT_GUARD_BROKEN,
            slot=None, role=role, evidence_ids=base_evidence + tuple(b.bar_id for b in context_window),
            source=(role, context_window), decision_at=decision_at,
        )
    search_bars = bars[first_index:candidate_index + 1]
    direction = BreakDirection.UP if role.side is Side.LONG else BreakDirection.DOWN
    ordered_candidates: list[CloseBreakCandidate] = []
    for bar in search_bars:
        numeric = candidates_by_bar.get(bar.bar_id)
        if numeric is None or numeric.status is ThresholdStatus.UNRESOLVED:
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.INCOMPLETE_FIRST_CROSS_HISTORY,
                slot=None, role=role, evidence_ids=base_evidence,
                source=(role, search_bars, tuple(_r2a_semantic_snapshot(c) for c in numeric_candidates)),
                decision_at=decision_at,
            )
        bar_index = next(index for index, item in enumerate(bars) if item.bar_id == bar.bar_id)
        expected_previous_id = bars[bar_index - 1].bar_id if bar_index else None
        if (numeric.break_bar_id != bar.bar_id
                or numeric.previous_bar_id != expected_previous_id
                or (numeric.symbol, numeric.timeframe) != (bar.symbol, bar.timeframe)
                or numeric.source_at != bar.close_time
                or numeric.confirmed_at != bar.close_time):
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.NUMERIC_EVIDENCE_UNRESOLVED,
                slot=None, role=role, evidence_ids=base_evidence,
                source=(role, bar, _r2a_semantic_snapshot(numeric)),
                decision_at=decision_at,
            )
        if numeric.available_at is None or numeric.available_at > decision_at:
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.FUTURE_EVIDENCE,
                slot=None, role=role, evidence_ids=base_evidence,
                source=(role, _r2a_semantic_snapshot(numeric)),
                decision_at=decision_at,
            )
        if numeric.variant_hash != variant.r2a_variant_hash:
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.NUMERIC_VARIANT_MISMATCH,
                slot=None, role=role, evidence_ids=base_evidence,
                source=(role, _r2a_semantic_snapshot(numeric)),
                decision_at=decision_at,
            )
        if (numeric.source_commit != variant.source_commit
                or numeric.price_basis != variant.verified_feed_basis_id):
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.NUMERIC_VARIANT_MISMATCH,
                slot=None, role=role, evidence_ids=base_evidence,
                source=(role, _r2a_semantic_snapshot(numeric)),
                decision_at=decision_at,
            )
        if numeric.broken_pivot_id != reference.pivot_id:
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.NUMERIC_REFERENCE_MISMATCH,
                slot=None, role=role, evidence_ids=base_evidence,
                source=(role, _r2a_semantic_snapshot(numeric)),
                decision_at=decision_at,
            )
        if numeric.direction is not direction:
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.NUMERIC_DIRECTION_MISMATCH,
                slot=None, role=role, evidence_ids=base_evidence,
                source=(role, _r2a_semantic_snapshot(numeric)),
                decision_at=decision_at,
            )
        if not _numeric_observation_matches_bars(numeric, bars[bar_index - 1], bar):
            return _evaluation(
                outcome=R2BOutcome.UNRESOLVED,
                reason=R2BReason.NUMERIC_EVIDENCE_UNRESOLVED,
                slot=None, role=role, evidence_ids=base_evidence,
                source=(role, bar, _r2a_semantic_snapshot(numeric)),
                decision_at=decision_at,
            )
        ordered_candidates.append(numeric)
    earlier_pass = next((item for item in ordered_candidates[:-1]
                         if item.status is ThresholdStatus.THRESHOLD_PASSED), None)
    semantic = tuple(_r2a_semantic_snapshot(item) for item in ordered_candidates)
    all_evidence = base_evidence + tuple(
        evidence_id for item in ordered_candidates for evidence_id in item.parent_evidence_ids
    )
    if earlier_pass is not None:
        return _evaluation(
            outcome=R2BOutcome.DISQUALIFIED,
            reason=R2BReason.REPEATED_INITIAL_CROSS,
            slot=None, role=role, evidence_ids=all_evidence,
            source=(role, semantic), decision_at=decision_at,
        )
    current = ordered_candidates[-1]
    outcome = (R2BOutcome.INITIAL_BREAK_CONFIRMED
               if current.status is ThresholdStatus.THRESHOLD_PASSED
               else R2BOutcome.FINAL_NO_CROSS)
    return _evaluation(
        outcome=outcome, reason=None, slot=current_slot, role=role,
        evidence_ids=all_evidence, source=(role, semantic), decision_at=decision_at,
    )


def resolve_protection_candidates(
    *,
    continuation_event_id: str,
    side: Side,
    reference_pivot: ConfirmedPivot,
    q_candidates: tuple[ConfirmedPivot, ...],
    break_bar: ClosedBar,
    path_bars: tuple[ClosedBar, ...],
    epoch: FrozenStructuralEpoch,
    variant: R2BVariant,
    decision_at: datetime,
) -> ProtectionCandidateLedger:
    """Freeze every Q assessment at B, then apply UNIQUE_ONLY."""
    require_utc(decision_at, "decision_at")
    if (not isinstance(side, Side) or type(reference_pivot) is not ConfirmedPivot
            or not isinstance(q_candidates, tuple)
            or any(type(q) is not ConfirmedPivot for q in q_candidates)
            or type(break_bar) is not ClosedBar
            or not isinstance(path_bars, tuple)
            or any(type(bar) is not ClosedBar for bar in path_bars)
            or not isinstance(epoch, FrozenStructuralEpoch)
            or not isinstance(variant, R2BVariant)):
        raise TypeError("typed protection inputs required")
    if (not _complete_bar_path(path_bars, decision_at)
            or not _pivots_match_source_bars(
                (reference_pivot, *q_candidates), path_bars
            )):
        assessments = tuple(
            ProtectionCandidateAssessment(q, False, R2BReason.INCOMPLETE_PROTECTION_PATH)
            for q in q_candidates
        )
    else:
        expected_type = PivotType.LOW if side is Side.LONG else PivotType.HIGH
        assessments_list: list[ProtectionCandidateAssessment] = []
        for q in sorted(q_candidates, key=lambda item: (item.source_at, item.pivot_id)):
            reason = None
            if ((q.symbol, q.timeframe) != (variant.symbol, variant.timeframe)
                    or (break_bar.symbol, break_bar.timeframe) != (variant.symbol, variant.timeframe)
                    or epoch.variant_hash != variant.variant_hash):
                reason = R2BReason.MIXED_CONTEXT
            elif q.pivot_type is not expected_type or not (
                reference_pivot.source_at < q.source_at < break_bar.open_time
            ):
                reason = R2BReason.PIVOT_OUTSIDE_FORMATION_LEG
            elif q.available_at >= break_bar.open_time or q.available_at > decision_at:
                reason = R2BReason.PIVOT_NOT_AVAILABLE_BEFORE_BREAK
            else:
                q_path = tuple(bar for bar in path_bars
                               if q.confirmed_at <= bar.close_time <= break_bar.close_time)
                if not q_path:
                    reason = R2BReason.INCOMPLETE_PROTECTION_PATH
                elif any(
                    bar.close < q.price if side is Side.LONG else bar.close > q.price
                    for bar in q_path
                ):
                    reason = R2BReason.PROTECTION_CANDIDATE_BREACHED
            assessments_list.append(ProtectionCandidateAssessment(q, reason is None, reason))
        assessments = tuple(assessments_list)
    qualified = tuple(item for item in assessments if item.qualified)
    if not qualified:
        outcome, reason, selected = (
            R2BOutcome.UNRESOLVED, R2BReason.NO_QUALIFYING_PIVOT, None,
        )
    elif len(qualified) == 1:
        outcome, reason, selected = (
            R2BOutcome.PROMOTION_ELIGIBLE_CANDIDATE,
            None,
            qualified[0].pivot.pivot_id,
        )
    else:
        outcome, reason, selected = (
            R2BOutcome.UNRESOLVED,
            R2BReason.MULTIPLE_CANDIDATE_LEVELS,
            None,
        )
    return ProtectionCandidateLedger(
        continuation_event_id=continuation_event_id,
        break_bar_id=break_bar.bar_id,
        assessments=assessments,
        outcome=outcome,
        reason=reason,
        selected_pivot_id=selected,
        epoch_id=epoch.epoch_id,
        variant_hash=variant.variant_hash,
    )
