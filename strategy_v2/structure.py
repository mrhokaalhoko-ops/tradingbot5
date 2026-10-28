"""Deterministic, prefix-stable observations from supplied closed bars."""

from datetime import datetime

from .causality import CausalTimes, require_utc
from .events import canonical_sha256
from .market_data import ClosedBar, validate_bar_series
from .structure_models import (BreakDirection, BreakMode, ConfirmedPivot,
                               PivotType, ProtectionResolution, ProtectionStatus,
                               StructureBreak, StructureLabel, StructurePoint,
                               StructureSpec, make_break_id, make_pivot_id,
                               make_structure_point_id)


def _pivot_sort(pivot: ConfirmedPivot):
    return (pivot.available_at, pivot.source_at, pivot.pivot_type.value, pivot.pivot_id)


def _validate_pivots(pivots: tuple[ConfirmedPivot, ...], spec: StructureSpec,
                     *, as_of: datetime) -> tuple[ConfirmedPivot, ...]:
    require_utc(as_of, "as_of")
    if not isinstance(spec, StructureSpec):
        raise TypeError("spec must be StructureSpec")
    if not isinstance(pivots, tuple) or any(type(item) is not ConfirmedPivot for item in pivots):
        raise TypeError("pivots must be a tuple of ConfirmedPivot")
    spec_id = canonical_sha256(spec)
    ids: set[str] = set()
    for item in pivots:
        if item.pivot_id in ids:
            raise ValueError("duplicate pivot_id")
        ids.add(item.pivot_id)
        if item.spec_id != spec_id or item.rule_version != spec.rule_version:
            raise ValueError("pivot spec mismatch")
        if item.available_at > as_of:
            raise ValueError("future unavailable pivot")
        if pivots and (item.symbol != pivots[0].symbol or item.timeframe != pivots[0].timeframe):
            raise ValueError("mixed pivot symbol or timeframe")
        if not item.parent_evidence_ids or item.source_bar_id not in item.parent_evidence_ids:
            raise ValueError("pivot missing source evidence")
    return tuple(sorted(pivots, key=_pivot_sort))


def detect_confirmed_pivots(bars: tuple[ClosedBar, ...], spec: StructureSpec,
                            *, as_of: datetime) -> tuple[ConfirmedPivot, ...]:
    """Earliest near-equal extremum wins; a right bar is needed for confirmation."""
    validate_bar_series(bars, as_of=as_of)
    if not isinstance(spec, StructureSpec):
        raise TypeError("spec must be StructureSpec")
    left, right = spec.pivot_radius_left, spec.pivot_radius_right
    tolerance = spec.equal_price_tolerance
    spec_id = canonical_sha256(spec)
    result: list[ConfirmedPivot] = []
    for i in range(left, len(bars) - right):
        source = bars[i]
        preceding = bars[i - left:i]
        following = bars[i + 1:i + right + 1]
        window = bars[i - left:i + right + 1]
        confirmation = bars[i + right]
        source_window_hash = canonical_sha256(window)
        for kind in (PivotType.HIGH, PivotType.LOW):
            price = source.high if kind is PivotType.HIGH else source.low
            if kind is PivotType.HIGH:
                eligible = (all(price > bar.high + tolerance for bar in preceding)
                            and all(price >= bar.high - tolerance for bar in following))
            else:
                eligible = (all(price < bar.low - tolerance for bar in preceding)
                            and all(price <= bar.low + tolerance for bar in following))
            if not eligible:
                continue
            times = CausalTimes(source_at=source.close_time,
                                confirmed_at=confirmation.close_time,
                                available_at=confirmation.available_at)
            parents = tuple(bar.bar_id for bar in window)
            evidence = dict(
                pivot_type=kind, price=price, source_bar_id=source.bar_id,
                confirmation_bar_id=confirmation.bar_id, symbol=source.symbol,
                timeframe=source.timeframe, times=times,
                parent_evidence_ids=parents, source_window_hash=source_window_hash,
                spec_id=spec_id, rule_version=spec.rule_version,
            )
            result.append(ConfirmedPivot(pivot_id=make_pivot_id(**evidence),
                                         **evidence))
    return tuple(sorted(result, key=_pivot_sort))


def classify_structure(pivots: tuple[ConfirmedPivot, ...], spec: StructureSpec,
                       *, as_of: datetime) -> tuple[StructurePoint, ...]:
    """Compare each confirmed pivot only with the prior same-type pivot."""
    ordered = _validate_pivots(pivots, spec, as_of=as_of)
    previous: dict[PivotType, ConfirmedPivot] = {}
    points: list[StructurePoint] = []
    for pivot in ordered:
        prior = previous.get(pivot.pivot_type)
        if prior is None:
            label = StructureLabel.UNCLASSIFIED
        else:
            delta = pivot.price - prior.price
            if abs(delta) <= spec.equal_price_tolerance:
                label = (StructureLabel.EQUAL_HIGH if pivot.pivot_type is PivotType.HIGH
                         else StructureLabel.EQUAL_LOW)
            elif pivot.pivot_type is PivotType.HIGH:
                label = StructureLabel.HH if delta > 0 else StructureLabel.LH
            else:
                label = StructureLabel.HL if delta > 0 else StructureLabel.LL
        parents = (pivot.pivot_id,) if prior is None else (prior.pivot_id, pivot.pivot_id)
        evidence = dict(
            label=label, pivot_id=pivot.pivot_id,
            prior_pivot_id=prior.pivot_id if prior else None,
            price=pivot.price, symbol=pivot.symbol, timeframe=pivot.timeframe,
            times=pivot.times, parent_evidence_ids=parents,
            spec_id=pivot.spec_id, rule_version=spec.rule_version,
        )
        points.append(StructurePoint(
            point_id=make_structure_point_id(**evidence), **evidence))
        previous[pivot.pivot_type] = pivot
    return tuple(points)


def detect_structure_breaks(bars: tuple[ClosedBar, ...],
                            pivots: tuple[ConfirmedPivot, ...],
                            spec: StructureSpec, *, as_of: datetime) -> tuple[StructureBreak, ...]:
    """Observe generic close crossings; do not name them BOS or CHoCH."""
    validate_bar_series(bars, as_of=as_of)
    ordered = _validate_pivots(pivots, spec, as_of=as_of)
    if bars and ordered and (bars[0].symbol != ordered[0].symbol or
                             bars[0].timeframe != ordered[0].timeframe):
        raise ValueError("bars and pivots must share symbol/timeframe")
    result: list[StructureBreak] = []
    for previous, bar in zip(bars, bars[1:]):
        for pivot in ordered:
            if (pivot.confirmed_at >= bar.close_time or
                    pivot.available_at > bar.available_at):
                continue
            if pivot.pivot_type is PivotType.HIGH:
                direction = BreakDirection.UP
                threshold = pivot.price + spec.close_break_buffer
                crossed = previous.close <= threshold < bar.close
            else:
                direction = BreakDirection.DOWN
                threshold = pivot.price - spec.close_break_buffer
                if threshold <= 0:
                    raise ValueError("break threshold must remain positive")
                crossed = previous.close >= threshold > bar.close
            if not crossed:
                continue
            times = CausalTimes(bar.close_time, bar.close_time, bar.available_at)
            parents = (pivot.pivot_id, previous.bar_id, bar.bar_id)
            break_support_hash = canonical_sha256((pivot, previous, bar))
            evidence = dict(
                broken_level_id=pivot.pivot_id, break_direction=direction,
                break_mode=BreakMode.CLOSE_ONLY, break_bar_id=bar.bar_id,
                price=bar.close, threshold=threshold, symbol=bar.symbol,
                timeframe=bar.timeframe, times=times,
                parent_evidence_ids=parents,
                break_support_hash=break_support_hash, spec_id=pivot.spec_id,
                rule_version=spec.rule_version,
            )
            result.append(StructureBreak(
                break_id=make_break_id(**evidence), **evidence))
    return tuple(sorted(result, key=lambda item: (item.available_at,
                                                   item.break_bar_id,
                                                   item.broken_level_id,
                                                   item.break_id)))


def derive_protected_levels(pivots: tuple[ConfirmedPivot, ...], spec: StructureSpec,
                            *, as_of: datetime) -> ProtectionResolution:
    """R1 cannot promote a pivot without a thesis and continuation/BOS event."""
    _validate_pivots(pivots, spec, as_of=as_of)
    return ProtectionResolution(ProtectionStatus.UNRESOLVED,
                                "THESIS_AND_CONTINUATION_EVIDENCE_NOT_DEFINED_IN_R1", ())
