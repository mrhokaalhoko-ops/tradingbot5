"""Pure R2A numeric close-cross candidate evaluation, never BOS classification."""

from datetime import datetime
from decimal import Decimal, DecimalException
from fractions import Fraction

from .causality import require_utc
from .events import canonical_sha256
from .market_data import ClosedBar, validate_bar_series
from .r2a_models import (ATRSourcePolicy, BOSInputSpec, CloseBreakCandidate,
                         CausalQualificationResolution, EvidenceResolutionReason as Reason,
                         InstrumentTickObservation, ThresholdStatus,
                         VolatilityObservation)
from .structure import detect_confirmed_pivots
from .structure_models import BreakDirection, ConfirmedPivot, PivotType, StructureSpec


class R2AContractError(ValueError):
    """A decision timestamp is mandatory to anchor even a rejected observation."""


class _NumericThresholdError(ArithmeticError):
    """An input or exact result exceeds the versioned research numeric domain."""


# EXACT_FRACTION_BOUNDED_V1: no ambient Decimal precision or rounding is used.
# These bounds are deliberately much wider than exchange price/ATR magnitudes.
_MAX_INPUT_DIGITS = 64
_MAX_INPUT_EXPONENT = 64
_MAX_RESULT_DIGITS = 256
_MAX_RESULT_SCALE = 128


def _bounded_fraction(value: Decimal) -> Fraction:
    parts = value.as_tuple()
    if (len(parts.digits) > _MAX_INPUT_DIGITS or
            abs(parts.exponent) > _MAX_INPUT_EXPONENT):
        raise _NumericThresholdError("input outside EXACT_FRACTION_BOUNDED_V1 domain")
    return Fraction(value)


def _exact_decimal(value: Fraction) -> Decimal:
    """Represent a terminating rational exactly, without Decimal arithmetic."""
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise _NumericThresholdError("nonterminating decimal result")
    scale = max(twos, fives)
    if scale > _MAX_RESULT_SCALE:
        raise _NumericThresholdError("result scale outside numeric policy")
    coefficient = abs(value.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    digits = str(coefficient)
    if len(digits) > _MAX_RESULT_DIGITS:
        raise _NumericThresholdError("result digits outside numeric policy")
    return Decimal((int(value < 0), tuple(map(int, digits)), -scale))


def _input_shape_failure(inputs: dict[str, object]) -> tuple[Reason, str] | None:
    """Inspect untrusted shapes before hashing or accessing evidence fields."""
    for name in ("bars", "pivot_window", "atr_window"):
        value = inputs[name]
        if value is None:
            continue
        if not isinstance(value, tuple):
            return Reason.INVALID_EVIDENCE_TYPE, name
        if any(type(item) is not ClosedBar for item in value):
            return Reason.INVALID_EVIDENCE_STRUCTURE, name
        if name == "bars" and len(value) not in (0, 2):
            return Reason.INVALID_EVIDENCE_STRUCTURE, name
    for name, expected in (
        ("pivot", ConfirmedPivot), ("structure_spec", StructureSpec),
        ("atr", VolatilityObservation), ("tick", InstrumentTickObservation),
        ("spec", BOSInputSpec),
    ):
        value = inputs[name]
        if value is not None and not isinstance(value, expected):
            return Reason.INVALID_EVIDENCE_TYPE, name
    return None


def _invalid_input_hash(name: str, reason: Reason, value: object,
                        decision_at: datetime) -> str:
    """Hash serializable malformed input; never call repr() on opaque objects."""
    try:
        supplied_hash = canonical_sha256(value)
    except (TypeError, ValueError, RecursionError, OverflowError):
        supplied_hash = None
    return canonical_sha256(("INVALID_R2A_INPUT", name, reason,
                             type(value).__module__, type(value).__qualname__,
                             supplied_hash, decision_at))


def _contiguous(bars: tuple[ClosedBar, ...], *, decision_at: datetime) -> bool:
    if not isinstance(bars, tuple) or not bars or any(type(bar) is not ClosedBar for bar in bars):
        return False
    validate_bar_series(bars, as_of=decision_at)
    duration = bars[0].close_time - bars[0].open_time
    return all(bar.close_time - bar.open_time == duration for bar in bars) and all(
        current.open_time == previous.close_time
        for previous, current in zip(bars, bars[1:])
    )


def _same_id_same_content(*series: tuple[ClosedBar, ...]) -> bool:
    seen: dict[str, ClosedBar] = {}
    for bars in series:
        for bar in bars:
            old = seen.setdefault(bar.bar_id, bar)
            if old != bar:
                return False
    return True


def _on_grid(value: Decimal, tick: Decimal) -> bool:
    return (_bounded_fraction(value) / _bounded_fraction(tick)).denominator == 1


def evaluate_close_break_candidate(
    bars: tuple[ClosedBar, ...] | None, *,
    pivot: ConfirmedPivot | None,
    pivot_window: tuple[ClosedBar, ...] | None,
    structure_spec: StructureSpec | None,
    atr: VolatilityObservation | None,
    atr_window: tuple[ClosedBar, ...] | None,
    tick: InstrumentTickObservation | None,
    spec: BOSInputSpec | None,
    decision_at: datetime,
) -> CloseBreakCandidate:
    """Use supplied final bars only; source evidence must be independently resolved.

    ATR is a versioned external observation. This resolver verifies its input
    window and timing, but does not assert an ATR formula or select a canonical
    ATR source. Numeric pass is deliberately not a structural BOS verdict.
    """
    try:
        require_utc(decision_at, "decision_at")
    except ValueError as exc:
        raise R2AContractError("decision_at must be an aware UTC timestamp") from exc
    inputs = dict(bars=bars, pivot=pivot, pivot_window=pivot_window,
                  structure_spec=structure_spec, atr=atr, atr_window=atr_window,
                  tick=tick, spec=spec)
    malformed = _input_shape_failure(inputs)
    if malformed is None:
        basis_hash = canonical_sha256((bars, pivot, pivot_window, structure_spec,
                                       atr, atr_window, tick, spec, decision_at))
    else:
        reason, name = malformed
        basis_hash = _invalid_input_hash(name, reason, inputs[name], decision_at)
    previous, current = (bars if isinstance(bars, tuple) and len(bars) == 2
                         and all(type(item) is ClosedBar for item in bars)
                         else (None, None))
    parents = tuple(value for value in (
        pivot.pivot_id if isinstance(pivot, ConfirmedPivot) else None,
        getattr(previous, "bar_id", None),
        getattr(current, "bar_id", None),
        atr.evidence_id if isinstance(atr, VolatilityObservation) else None,
        tick.evidence_id if isinstance(tick, InstrumentTickObservation) else None,
    ) if isinstance(value, str) and value)

    def result(status: ThresholdStatus, reason: Reason | None,
               *, delta: Decimal | None = None, threshold: Decimal | None = None,
               support_hash: str | None = None,
               available_at: datetime | None = None) -> CloseBreakCandidate:
        # An unresolved record cannot claim a future observation as already available.
        if status is ThresholdStatus.UNRESOLVED:
            available_at = None
        return CloseBreakCandidate(
            status=status, reason=reason,
            direction=(BreakDirection.UP if pivot.pivot_type is PivotType.HIGH
                       else BreakDirection.DOWN) if isinstance(pivot, ConfirmedPivot) else None,
            symbol=current.symbol if current else None,
            timeframe=current.timeframe if current else None,
            previous_bar_id=previous.bar_id if previous else None,
            break_bar_id=current.bar_id if current else None,
            broken_pivot_id=pivot.pivot_id if isinstance(pivot, ConfirmedPivot) else None,
            atr_evidence_id=atr.evidence_id if isinstance(atr, VolatilityObservation) else None,
            tick_evidence_id=tick.evidence_id if isinstance(tick, InstrumentTickObservation) else None,
            threshold_delta=delta, threshold_price=threshold,
            pivot_source_window_hash=pivot.source_window_hash if isinstance(pivot, ConfirmedPivot) else None,
            break_support_hash=support_hash,
            variant_hash=spec.variant_hash if isinstance(spec, BOSInputSpec) else None,
            threshold_policy_id=spec.threshold_policy_id if isinstance(spec, BOSInputSpec) else None,
            rule_version=spec.rule_version if isinstance(spec, BOSInputSpec) else None,
            source_commit=spec.source_commit if isinstance(spec, BOSInputSpec) else None,
            atr_source_policy=spec.atr_source_policy if isinstance(spec, BOSInputSpec) else None,
            atr_method_id=spec.atr_method_id if isinstance(spec, BOSInputSpec) else None,
            atr_period=spec.atr_period if isinstance(spec, BOSInputSpec) else None,
            atr_policy_id=spec.atr_policy_id if isinstance(spec, BOSInputSpec) else None,
            price_basis=spec.price_basis if isinstance(spec, BOSInputSpec) else None,
            source_at=current.close_time if current else None,
            confirmed_at=current.close_time if current else None,
            available_at=available_at, decision_at=decision_at,
            parent_evidence_ids=parents if len(set(parents)) == len(parents) else (),
            evidence_hash=basis_hash,
            qualification=CausalQualificationResolution(
                ThresholdStatus.UNRESOLVED, Reason.STRUCTURE_ROLE_NOT_PROVEN),
        )

    if malformed is not None:
        return result(ThresholdStatus.UNRESOLVED, malformed[0])
    if spec is None:
        return result(ThresholdStatus.UNRESOLVED, Reason.MISSING_SPEC)
    if not isinstance(spec, BOSInputSpec) or not isinstance(structure_spec, StructureSpec):
        return result(ThresholdStatus.UNRESOLVED, Reason.SPEC_MISMATCH)
    if pivot is None:
        return result(ThresholdStatus.UNRESOLVED, Reason.MISSING_PIVOT)
    if not isinstance(pivot, ConfirmedPivot):
        return result(ThresholdStatus.UNRESOLVED, Reason.PIVOT_SOURCE_MISMATCH)
    if atr is None:
        return result(ThresholdStatus.UNRESOLVED, Reason.MISSING_ATR)
    if not isinstance(atr, VolatilityObservation):
        return result(ThresholdStatus.UNRESOLVED, Reason.ATR_SOURCE_MISMATCH)
    if (atr.method != spec.atr_method_id or atr.period != spec.atr_period or
            atr.policy_id != spec.atr_policy_id):
        return result(ThresholdStatus.UNRESOLVED, Reason.ATR_VARIANT_MISMATCH)
    if tick is None:
        return result(ThresholdStatus.UNRESOLVED, Reason.MISSING_TICK)
    if not isinstance(tick, InstrumentTickObservation):
        return result(ThresholdStatus.UNRESOLVED, Reason.TICK_NOT_EFFECTIVE)
    if previous is None or current is None or pivot_window is None or atr_window is None:
        return result(ThresholdStatus.UNRESOLVED, Reason.MISSING_SOURCE_BARS)
    if len(set(parents)) != len(parents):
        return result(ThresholdStatus.UNRESOLVED, Reason.DUPLICATE_EVIDENCE)
    if any(item.available_at > decision_at for item in (*bars, pivot, atr, tick)):
        return result(ThresholdStatus.UNRESOLVED, Reason.FUTURE_EVIDENCE)
    if any(bar.available_at > decision_at for window in (pivot_window, atr_window)
           for bar in window):
        return result(ThresholdStatus.UNRESOLVED, Reason.FUTURE_EVIDENCE)
    try:
        valid_series = all(_contiguous(window, decision_at=decision_at)
                           for window in (bars, pivot_window, atr_window))
    except (TypeError, ValueError):
        valid_series = False
    if not valid_series:
        return result(ThresholdStatus.UNRESOLVED, Reason.INVALID_BAR_SEQUENCE)
    if not _same_id_same_content(bars, pivot_window):
        return result(ThresholdStatus.UNRESOLVED, Reason.PIVOT_SOURCE_MISMATCH)
    if not _same_id_same_content(bars, atr_window) or not _same_id_same_content(pivot_window, atr_window):
        return result(ThresholdStatus.UNRESOLVED, Reason.ATR_SOURCE_MISMATCH)
    if any((bar.symbol, bar.timeframe) != (current.symbol, current.timeframe)
           for window in (bars, pivot_window, atr_window) for bar in window):
        return result(ThresholdStatus.UNRESOLVED, Reason.MIXED_CONTEXT)
    if any(item.symbol != current.symbol for item in (pivot, atr, tick, spec)) or any(
        item.timeframe != current.timeframe for item in (pivot, atr, spec)
    ) or atr.price_basis != spec.price_basis or tick.price_basis != spec.price_basis:
        return result(ThresholdStatus.UNRESOLVED, Reason.MIXED_CONTEXT)
    if (spec.structure_spec_hash != canonical_sha256(structure_spec) or
            pivot.spec_id != spec.structure_spec_hash or
            pivot.rule_version != structure_spec.rule_version):
        return result(ThresholdStatus.UNRESOLVED, Reason.SPEC_MISMATCH)
    left, right = structure_spec.pivot_radius_left, structure_spec.pivot_radius_right
    if (len(pivot_window) != left + right + 1 or
            pivot_window[left].bar_id != pivot.source_bar_id or
            pivot_window[-1].bar_id != pivot.confirmation_bar_id or
            tuple(bar.bar_id for bar in pivot_window) != pivot.parent_evidence_ids or
            canonical_sha256(pivot_window) != pivot.source_window_hash):
        return result(ThresholdStatus.UNRESOLVED, Reason.PIVOT_SOURCE_MISMATCH)
    try:
        recomputed = detect_confirmed_pivots(pivot_window, structure_spec,
                                             as_of=decision_at)
    except (TypeError, ValueError):
        return result(ThresholdStatus.UNRESOLVED, Reason.PIVOT_SOURCE_MISMATCH)
    if pivot not in recomputed or pivot.confirmed_at > current.close_time:
        return result(ThresholdStatus.UNRESOLVED, Reason.PIVOT_SOURCE_MISMATCH)
    if (len(atr_window) < atr.period or
            canonical_sha256(atr_window) != atr.source_bars_hash or
            atr.window_end_at != atr_window[-1].close_time):
        return result(ThresholdStatus.UNRESOLVED, Reason.ATR_SOURCE_MISMATCH)
    expected_end = (previous.close_time if spec.atr_source_policy is ATRSourcePolicy.PRE_BREAK_CLOSED
                    else current.close_time)
    expected_bar = previous if spec.atr_source_policy is ATRSourcePolicy.PRE_BREAK_CLOSED else current
    if atr.window_end_at != expected_end or atr_window[-1] != expected_bar:
        return result(ThresholdStatus.UNRESOLVED, Reason.ATR_WINDOW_POLICY_MISMATCH)
    if tick.effective_at > current.open_time:
        return result(ThresholdStatus.UNRESOLVED, Reason.TICK_NOT_EFFECTIVE)
    try:
        if any(not _on_grid(price, tick.tick_size) for bar in bars for price in
               (bar.open, bar.high, bar.low, bar.close)) or not _on_grid(pivot.price, tick.tick_size):
            return result(ThresholdStatus.UNRESOLVED, Reason.OFF_GRID_PRICE)
        tick_fraction = _bounded_fraction(tick.tick_size)
        raw_delta_fraction = max(_bounded_fraction(tick.tick_floor),
                                 _bounded_fraction(spec.multiplier_b) * _bounded_fraction(atr.atr))
        direction = BreakDirection.UP if pivot.pivot_type is PivotType.HIGH else BreakDirection.DOWN
        pivot_fraction = _bounded_fraction(pivot.price)
        raw_threshold = (pivot_fraction + raw_delta_fraction if direction is BreakDirection.UP
                         else pivot_fraction - raw_delta_fraction)
        if raw_threshold <= 0:
            return result(ThresholdStatus.UNRESOLVED, Reason.NONPOSITIVE_THRESHOLD)
        grid_ratio = raw_threshold / tick_fraction
        grid_index = (-(-grid_ratio.numerator // grid_ratio.denominator)
                      if direction is BreakDirection.UP
                      else grid_ratio.numerator // grid_ratio.denominator)
        threshold_fraction = grid_index * tick_fraction
        if threshold_fraction <= 0:
            return result(ThresholdStatus.UNRESOLVED, Reason.NONPOSITIVE_THRESHOLD)
        raw_delta = _exact_decimal(raw_delta_fraction)
        threshold = _exact_decimal(threshold_fraction)
    except (_NumericThresholdError, DecimalException, OverflowError):
        return result(ThresholdStatus.UNRESOLVED, Reason.NUMERIC_THRESHOLD_UNCOMPUTABLE)
    crossed = (previous.close <= threshold < current.close if direction is BreakDirection.UP
               else previous.close >= threshold > current.close)
    support_hash = canonical_sha256((previous, current, pivot, atr, tick, spec))
    return result(ThresholdStatus.THRESHOLD_PASSED if crossed else
                  ThresholdStatus.THRESHOLD_NOT_PASSED,
                  None if crossed else Reason.NO_CLOSE_CROSS,
                  delta=raw_delta, threshold=threshold, support_hash=support_hash,
                  available_at=max(current.available_at, pivot.available_at,
                                   atr.available_at, tick.available_at))
