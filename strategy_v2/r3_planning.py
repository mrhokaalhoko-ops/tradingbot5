"""Pure causal stop and target planning; no orders, sizing, or exchange writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from .causality import require_utc
from .enums import InvalidationScope, Side
from .events import canonical_sha256
from .models import ExitTarget, InvalidationLevel, RewardReference, ThesisId


_MAX_INPUT_DIGITS = 64
_MAX_INPUT_EXPONENT = 64
_MAX_RESULT_DIGITS = 256
_MAX_RESULT_SCALE = 128


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _decimal(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be finite Decimal")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'}")


def _exact_decimal(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("nonterminating decimal outside planning policy")
    scale = max(twos, fives)
    if scale > _MAX_RESULT_SCALE:
        raise ValueError("result scale outside planning numeric domain")
    coefficient = abs(value.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    if len(str(coefficient)) > _MAX_RESULT_DIGITS:
        raise ValueError("result digits outside planning numeric domain")
    digits = tuple(map(int, str(coefficient)))
    return Decimal((int(value < 0), digits, -scale))


def _bounded_fraction(value: Decimal) -> Fraction:
    parts = value.as_tuple()
    if len(parts.digits) > _MAX_INPUT_DIGITS or abs(parts.exponent) > _MAX_INPUT_EXPONENT:
        raise ValueError("input outside planning numeric domain")
    return Fraction(value)


def _floor_fraction(value: Fraction, decimal_places: int) -> Decimal:
    scale = 10 ** decimal_places
    scaled = value * scale
    coefficient = scaled.numerator // scaled.denominator
    return _exact_decimal(Fraction(coefficient, scale))


def _outward_grid(value: Fraction, tick: Fraction, side: Side) -> Decimal:
    ratio = value / tick
    index = (ratio.numerator // ratio.denominator if side is Side.LONG
             else -(-ratio.numerator // ratio.denominator))
    return _exact_decimal(index * tick)


class PlanStatus(str, Enum):
    READY = "READY"
    UNRESOLVED = "UNRESOLVED"
    REJECTED = "REJECTED"


class PlanningReason(str, Enum):
    NO_CAUSAL_INVALIDATION = "SKIP_NO_CAUSAL_INVALIDATION"
    MULTIPLE_CAUSAL_INVALIDATIONS = "MULTIPLE_CAUSAL_INVALIDATIONS"
    SELECTED_INVALIDATION_REJECTED = "SELECTED_INVALIDATION_REJECTED"
    FUTURE_INVALIDATION = "FUTURE_INVALIDATION"
    INVALID_STOP_GEOMETRY = "INVALID_STOP_GEOMETRY"
    NO_CAUSAL_TARGET = "NO_CAUSAL_TARGET"
    MULTIPLE_CAUSAL_TARGETS = "MULTIPLE_CAUSAL_TARGETS"
    SELECTED_TARGET_REJECTED = "SELECTED_TARGET_REJECTED"
    FUTURE_TARGET = "FUTURE_TARGET"
    TARGET_WRONG_SIDE = "TARGET_WRONG_SIDE"
    NET_RR_BELOW_MINIMUM = "NET_RR_BELOW_MINIMUM"
    NUMERIC_UNCOMPUTABLE = "NUMERIC_UNCOMPUTABLE"


class CandidateStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"


class TargetMode(str, Enum):
    STRUCTURAL_FINAL_TP = "STRUCTURAL_FINAL_TP"
    FULL_POSITION_RUNNER = "FULL_POSITION_RUNNER"


@dataclass(frozen=True, slots=True)
class StopPolicySpec:
    source_commit: str
    policy_id: str
    rule_version: str
    atr_multiplier: Decimal
    minimum_tick_count: int
    spread_multiplier: Decimal
    numeric_policy_id: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if len(self.source_commit) != 40:
            raise ValueError("full source commit required")
        for name in ("policy_id", "rule_version", "numeric_policy_id"):
            _text(getattr(self, name), name)
        _decimal(self.atr_multiplier, "atr_multiplier", allow_zero=True)
        _decimal(self.spread_multiplier, "spread_multiplier", allow_zero=True)
        if type(self.minimum_tick_count) is not int or self.minimum_tick_count < 1:
            raise ValueError("positive explicit minimum_tick_count required")
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.policy_id, self.rule_version,
            self.atr_multiplier, self.minimum_tick_count,
            self.spread_multiplier, self.numeric_policy_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class InvalidationAssessment:
    level: InvalidationLevel
    status: CandidateStatus
    reason: PlanningReason | None
    assessment_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.level, InvalidationLevel) or not isinstance(
            self.status, CandidateStatus
        ):
            raise TypeError("typed invalidation assessment required")
        if self.status is CandidateStatus.ELIGIBLE:
            if self.reason is not None:
                raise ValueError("eligible level cannot carry rejection")
        elif not isinstance(self.reason, PlanningReason):
            raise ValueError("rejected level needs typed reason")
        object.__setattr__(self, "assessment_id", "ias_" + canonical_sha256((
            self.level, self.status, self.reason,
        )).lower())


@dataclass(frozen=True, slots=True)
class InitialStopPlan:
    status: PlanStatus
    reason: PlanningReason | None
    thesis_id: ThesisId
    side: Side
    entry_price: Decimal
    assessments: tuple[InvalidationAssessment, ...]
    selected_level_id: str | None
    selected_invalidation_price: Decimal | None
    buffer: Decimal | None
    final_stop: Decimal | None
    risk_distance: Decimal | None
    policy_variant_hash: str
    decision_at: datetime
    plan_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, PlanStatus) or type(self.thesis_id) is not ThesisId:
            raise TypeError("typed stop status and thesis ID required")
        if not isinstance(self.side, Side):
            raise TypeError("typed side required")
        _decimal(self.entry_price, "entry_price")
        require_utc(self.decision_at, "decision_at")
        if self.status is PlanStatus.READY:
            if self.reason is not None or any(value is None for value in (
                self.selected_level_id, self.selected_invalidation_price,
                self.buffer, self.final_stop, self.risk_distance,
            )):
                raise ValueError("ready stop needs complete geometry")
            if self.side is Side.LONG and not self.final_stop < self.selected_invalidation_price < self.entry_price:
                raise ValueError("LONG stop geometry invalid")
            if self.side is Side.SHORT and not self.final_stop > self.selected_invalidation_price > self.entry_price:
                raise ValueError("SHORT stop geometry invalid")
        elif not isinstance(self.reason, PlanningReason):
            raise ValueError("non-ready stop requires typed reason")
        object.__setattr__(self, "plan_id", "stp_" + canonical_sha256((
            self.status, self.reason, self.thesis_id, self.side,
            self.entry_price, self.assessments, self.selected_level_id,
            self.selected_invalidation_price, self.buffer, self.final_stop,
            self.risk_distance, self.policy_variant_hash,
        )).lower())


def build_initial_stop_plan(
    *,
    thesis_id: ThesisId,
    side: Side,
    entry_price: Decimal,
    invalidations: tuple[InvalidationLevel, ...],
    required_scope: InvalidationScope,
    selected_level_id: str | None,
    atr: Decimal,
    tick_size: Decimal,
    spread: Decimal,
    policy: StopPolicySpec,
    decision_at: datetime,
) -> InitialStopPlan:
    require_utc(decision_at, "decision_at")
    if type(thesis_id) is not ThesisId or not isinstance(side, Side):
        raise TypeError("typed thesis ID and side required")
    if not isinstance(invalidations, tuple) or any(
        not isinstance(level, InvalidationLevel) for level in invalidations
    ):
        raise TypeError("typed immutable invalidations required")
    if not isinstance(required_scope, InvalidationScope) or not isinstance(policy, StopPolicySpec):
        raise TypeError("typed scope and policy required")
    for name, value, allow_zero in (
        ("entry_price", entry_price, False), ("atr", atr, False),
        ("tick_size", tick_size, False), ("spread", spread, True),
    ):
        _decimal(value, name, allow_zero=allow_zero)
    assessments = []
    for level in invalidations:
        reason = None
        if level.thesis_id != thesis_id or level.side is not side or level.scope is not required_scope:
            reason = PlanningReason.SELECTED_INVALIDATION_REJECTED
        elif level.times.available_at > decision_at:
            reason = PlanningReason.FUTURE_INVALIDATION
        elif ((side is Side.LONG and level.price >= entry_price)
              or (side is Side.SHORT and level.price <= entry_price)):
            reason = PlanningReason.INVALID_STOP_GEOMETRY
        assessments.append(InvalidationAssessment(
            level, CandidateStatus.ELIGIBLE if reason is None else CandidateStatus.REJECTED,
            reason,
        ))
    assessments_tuple = tuple(assessments)
    eligible = tuple(item for item in assessments_tuple if item.status is CandidateStatus.ELIGIBLE)
    selected = next((item for item in eligible
                     if item.level.level_id == selected_level_id), None)
    if selected_level_id is None:
        if len(eligible) == 1:
            selected = eligible[0]
        elif not eligible:
            reason = PlanningReason.NO_CAUSAL_INVALIDATION
        else:
            reason = PlanningReason.MULTIPLE_CAUSAL_INVALIDATIONS
    elif selected is None:
        reason = PlanningReason.SELECTED_INVALIDATION_REJECTED
    if selected is None:
        return InitialStopPlan(
            PlanStatus.UNRESOLVED, reason, thesis_id, side, entry_price,
            assessments_tuple, None, None, None, None, None,
            policy.variant_hash, decision_at,
        )
    try:
        entry_fraction = _bounded_fraction(entry_price)
        atr_buffer = _bounded_fraction(policy.atr_multiplier) * _bounded_fraction(atr)
        tick_fraction = _bounded_fraction(tick_size)
        tick_buffer = policy.minimum_tick_count * tick_fraction
        spread_buffer = _bounded_fraction(policy.spread_multiplier) * _bounded_fraction(spread)
        buffer_fraction = max(atr_buffer, tick_buffer, spread_buffer)
        level_fraction = _bounded_fraction(selected.level.price)
        raw = (level_fraction - buffer_fraction if side is Side.LONG
               else level_fraction + buffer_fraction)
        buffer = _exact_decimal(buffer_fraction)
        final_stop = _outward_grid(raw, tick_fraction, side) if raw > 0 else None
        if final_stop is not None:
            stop_fraction = Fraction(final_stop)
            risk_distance = _exact_decimal(
                entry_fraction - stop_fraction if side is Side.LONG
                else stop_fraction - entry_fraction
            )
        else:
            risk_distance = None
    except (ArithmeticError, ValueError, OverflowError):
        return InitialStopPlan(
            PlanStatus.UNRESOLVED, PlanningReason.NUMERIC_UNCOMPUTABLE,
            thesis_id, side, entry_price, assessments_tuple,
            selected.level.level_id, selected.level.price, None, None, None,
            policy.variant_hash, decision_at,
        )
    if raw <= 0:
        return InitialStopPlan(
            PlanStatus.REJECTED, PlanningReason.INVALID_STOP_GEOMETRY,
            thesis_id, side, entry_price, assessments_tuple,
            selected.level.level_id, selected.level.price, None, None, None,
            policy.variant_hash, decision_at,
        )
    if (risk_distance <= 0 or
            (side is Side.LONG and final_stop >= selected.level.price) or
            (side is Side.SHORT and final_stop <= selected.level.price)):
        return InitialStopPlan(
            PlanStatus.REJECTED, PlanningReason.INVALID_STOP_GEOMETRY,
            thesis_id, side, entry_price, assessments_tuple,
            selected.level.level_id, selected.level.price, buffer,
            final_stop, None, policy.variant_hash, decision_at,
        )
    return InitialStopPlan(
        PlanStatus.READY, None, thesis_id, side, entry_price,
        assessments_tuple, selected.level.level_id, selected.level.price,
        buffer, final_stop, risk_distance, policy.variant_hash, decision_at,
    )


@dataclass(frozen=True, slots=True)
class TargetPolicySpec:
    source_commit: str
    policy_id: str
    rule_version: str
    mode: TargetMode
    minimum_gross_rr: Decimal
    runner_exit_event_types: tuple[str, ...]
    rr_decimal_places: int
    numeric_policy_id: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if len(self.source_commit) != 40:
            raise ValueError("full source commit required")
        for name in ("policy_id", "rule_version"):
            _text(getattr(self, name), name)
        if not isinstance(self.mode, TargetMode):
            raise TypeError("typed target mode required")
        _decimal(self.minimum_gross_rr, "minimum_gross_rr", allow_zero=True)
        if self.mode is TargetMode.FULL_POSITION_RUNNER and not self.runner_exit_event_types:
            raise ValueError("runner requires explicit exit event types")
        if self.mode is TargetMode.STRUCTURAL_FINAL_TP and self.runner_exit_event_types:
            raise ValueError("structural final target cannot carry runner exits")
        if type(self.rr_decimal_places) is not int or not 0 <= self.rr_decimal_places <= 12:
            raise ValueError("rr_decimal_places must be explicit in [0,12]")
        _text(self.numeric_policy_id, "numeric_policy_id")
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.policy_id, self.rule_version, self.mode,
            self.minimum_gross_rr, self.runner_exit_event_types,
            self.rr_decimal_places, self.numeric_policy_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class TargetAssessment:
    target: ExitTarget
    status: CandidateStatus
    reason: PlanningReason | None
    assessment_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target, ExitTarget) or not isinstance(self.status, CandidateStatus):
            raise TypeError("typed target assessment required")
        if (self.status is CandidateStatus.ELIGIBLE) == (self.reason is not None):
            raise ValueError("target assessment status/reason mismatch")
        object.__setattr__(self, "assessment_id", "tas_" + canonical_sha256((
            self.target, self.status, self.reason,
        )).lower())


@dataclass(frozen=True, slots=True)
class CausalTargetPlan:
    status: PlanStatus
    reason: PlanningReason | None
    mode: TargetMode
    side: Side
    entry_price: Decimal
    stop_price: Decimal
    assessments: tuple[TargetAssessment, ...]
    selected_target: ExitTarget | None
    reward_reference: RewardReference | None
    gross_rr: Decimal | None
    runner_exit_event_types: tuple[str, ...]
    policy_variant_hash: str
    decision_at: datetime
    plan_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, PlanStatus) or not isinstance(self.mode, TargetMode):
            raise TypeError("typed target plan status and mode required")
        if not isinstance(self.side, Side):
            raise TypeError("typed side required")
        require_utc(self.decision_at, "decision_at")
        if self.status is PlanStatus.READY:
            if self.reason is not None or self.reward_reference is None:
                raise ValueError("ready target plan needs reward reference")
            if self.mode is TargetMode.STRUCTURAL_FINAL_TP and self.selected_target is None:
                raise ValueError("structural target plan needs selected target")
            if self.mode is TargetMode.FULL_POSITION_RUNNER and self.selected_target is not None:
                raise ValueError("runner cannot fabricate a target")
        elif not isinstance(self.reason, PlanningReason):
            raise ValueError("non-ready target plan needs typed reason")
        object.__setattr__(self, "plan_id", "tgp_" + canonical_sha256((
            self.status, self.reason, self.mode, self.side, self.entry_price,
            self.stop_price, self.assessments, self.selected_target,
            self.reward_reference, self.gross_rr, self.runner_exit_event_types,
            self.policy_variant_hash,
        )).lower())


def build_causal_target_plan(
    *,
    side: Side,
    entry_price: Decimal,
    stop_price: Decimal,
    targets: tuple[ExitTarget, ...],
    selected_target_id: str | None,
    reward_reference: RewardReference,
    policy: TargetPolicySpec,
    decision_at: datetime,
) -> CausalTargetPlan:
    require_utc(decision_at, "decision_at")
    if not isinstance(side, Side) or not isinstance(policy, TargetPolicySpec):
        raise TypeError("typed side and target policy required")
    if not isinstance(reward_reference, RewardReference) or reward_reference.side is not side:
        raise ValueError("causal reward reference with matching side required")
    if reward_reference.times.available_at > decision_at:
        raise ValueError("future reward reference forbidden")
    _decimal(entry_price, "entry_price")
    _decimal(stop_price, "stop_price")
    try:
        entry_fraction = _bounded_fraction(entry_price)
        stop_fraction = _bounded_fraction(stop_price)
        risk_fraction = (
            entry_fraction - stop_fraction if side is Side.LONG
            else stop_fraction - entry_fraction
        )
    except (ArithmeticError, ValueError, OverflowError):
        return CausalTargetPlan(
            PlanStatus.UNRESOLVED, PlanningReason.NUMERIC_UNCOMPUTABLE,
            policy.mode, side, entry_price, stop_price, (), None,
            reward_reference, None, policy.runner_exit_event_types,
            policy.variant_hash, decision_at,
        )
    if risk_fraction <= 0:
        raise ValueError("entry/stop geometry invalid")
    if policy.mode is TargetMode.FULL_POSITION_RUNNER:
        return CausalTargetPlan(
            PlanStatus.READY, None, policy.mode, side, entry_price, stop_price,
            (), None, reward_reference, None, policy.runner_exit_event_types,
            policy.variant_hash, decision_at,
        )
    if not isinstance(targets, tuple) or any(not isinstance(item, ExitTarget) for item in targets):
        raise TypeError("typed immutable targets required")
    assessments = []
    for target in targets:
        reason = None
        if target.side is not side:
            reason = PlanningReason.SELECTED_TARGET_REJECTED
        elif target.times.available_at > decision_at:
            reason = PlanningReason.FUTURE_TARGET
        elif ((side is Side.LONG and target.price <= entry_price)
              or (side is Side.SHORT and target.price >= entry_price)):
            reason = PlanningReason.TARGET_WRONG_SIDE
        assessments.append(TargetAssessment(
            target, CandidateStatus.ELIGIBLE if reason is None else CandidateStatus.REJECTED,
            reason,
        ))
    assessments_tuple = tuple(assessments)
    eligible = tuple(item for item in assessments_tuple if item.status is CandidateStatus.ELIGIBLE)
    selected = next((item for item in eligible
                     if item.target.target_id == selected_target_id), None)
    if selected_target_id is None:
        if len(eligible) == 1:
            selected = eligible[0]
        elif not eligible:
            reason = PlanningReason.NO_CAUSAL_TARGET
        else:
            reason = PlanningReason.MULTIPLE_CAUSAL_TARGETS
    elif selected is None:
        reason = PlanningReason.SELECTED_TARGET_REJECTED
    if selected is None:
        return CausalTargetPlan(
            PlanStatus.UNRESOLVED, reason, policy.mode, side, entry_price,
            stop_price, assessments_tuple, None, reward_reference, None, (),
            policy.variant_hash, decision_at,
        )
    try:
        target_fraction = _bounded_fraction(selected.target.price)
        reward_fraction = (
            target_fraction - entry_fraction if side is Side.LONG
            else entry_fraction - target_fraction
        )
        exact_rr = reward_fraction / risk_fraction
        gross_rr = _floor_fraction(exact_rr, policy.rr_decimal_places)
        below_minimum = exact_rr < _bounded_fraction(policy.minimum_gross_rr)
    except (ArithmeticError, ValueError, OverflowError):
        return CausalTargetPlan(
            PlanStatus.UNRESOLVED, PlanningReason.NUMERIC_UNCOMPUTABLE,
            policy.mode, side, entry_price, stop_price, assessments_tuple,
            selected.target, reward_reference, None, (), policy.variant_hash,
            decision_at,
        )
    if below_minimum:
        return CausalTargetPlan(
            PlanStatus.REJECTED, PlanningReason.NET_RR_BELOW_MINIMUM,
            policy.mode, side, entry_price, stop_price, assessments_tuple,
            selected.target, reward_reference, gross_rr, (),
            policy.variant_hash, decision_at,
        )
    return CausalTargetPlan(
        PlanStatus.READY, None, policy.mode, side, entry_price, stop_price,
        assessments_tuple, selected.target, reward_reference, gross_rr, (),
        policy.variant_hash, decision_at,
    )
