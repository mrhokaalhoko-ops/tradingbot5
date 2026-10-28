"""Deterministic fixtures for pivots, labels, close-crosses, and prefix causality."""

import ast
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_v2.events import EventEnvelope, InputEvidenceRef, canonical_sha256
from strategy_v2.causality import CausalTimes
from strategy_v2.market_data import ClosedBar
from strategy_v2.models import IdentityRefs, ThesisId
from strategy_v2.structure import (classify_structure, derive_protected_levels,
                                   detect_confirmed_pivots, detect_structure_breaks)
from strategy_v2.structure_models import (BreakDirection, BreakMode, EqualTiePolicy,
                                          PivotType, ProtectedLevel, ProtectionStatus, StructureLabel,
                                          StructureSpec, make_pivot_id)

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
D = Decimal


def bar(index: int, *, high="12", low="8", close="10", available_delay=0):
    start = BASE + timedelta(minutes=index)
    return ClosedBar(f"bar-{index}", "ABCUSDT", "1m", start,
                     start + timedelta(minutes=1),
                     start + timedelta(minutes=1 + available_delay),
                     D(close), D(high), D(low), D(close), D("5"), True)


def spec(*, left=1, right=1, tolerance="0", buffer="0", rule="r1"):
    return StructureSpec(left, right, D(tolerance), EqualTiePolicy.EARLIEST,
                         D(buffer), rule)


def series(highs, lows, closes=None, *, delay=0):
    closes = closes or [str((D(hi) + D(lo)) / 2) for hi, lo in zip(highs, lows)]
    return tuple(bar(i, high=str(hi), low=str(lo), close=str(cl),
                     available_delay=delay)
                 for i, (hi, lo, cl) in enumerate(zip(highs, lows, closes)))


def pivots(bars, config=None):
    config = config or spec()
    return detect_confirmed_pivots(bars, config, as_of=bars[-1].available_at)


def reidentify_pivot(pivot, **changes):
    """Construct deliberate timing probes with a canonical, consistent R1 ID."""
    evidence = {item.name: getattr(pivot, item.name) for item in fields(pivot)
                if item.name != "pivot_id"}
    evidence.update(changes)
    return replace(pivot, pivot_id=make_pivot_id(**evidence), **changes)


UP = series([10, 12, 11, 13, 12, 14, 13], [8, 9, 8.5, 10, 9.5, 11, 10.5])
DOWN = series([22, 21, 21.5, 20, 20.5, 19, 19.5], [20, 18, 19, 17, 18, 16, 17])
RANGE = series([10, 12, 11, 12, 11, 12, 10], [8, 9, 8, 9, 8, 9, 8])


def test_pivot_confirmation_uses_right_close_and_feed_availability():
    bars = series([10, 12, 11], [8, 9, 8.5], delay=2)
    assert detect_confirmed_pivots(bars[:2], spec(), as_of=bars[1].available_at) == ()
    high = next(p for p in pivots(bars) if p.pivot_type is PivotType.HIGH)
    assert high.source_bar_id == bars[1].bar_id
    assert high.confirmation_bar_id == bars[2].bar_id
    assert high.source_at == bars[1].close_time
    assert high.confirmed_at == bars[2].close_time
    assert high.available_at == bars[2].available_at > high.confirmed_at
    assert high.parent_evidence_ids == tuple(item.bar_id for item in bars)
    assert InputEvidenceRef(high.pivot_id, "closed OHLCV", high.times, "a" * 64).times == high.times


def test_pivot_evidence_round_trips_into_r0_event_envelope():
    pivot = pivots(UP)[0]
    ref = InputEvidenceRef(pivot.pivot_id, "closed OHLCV",
                           pivot.times, pivot.source_window_hash)
    envelope = EventEnvelope(
        schema_version="1.0", event_type="V2_STRUCTURE_EVIDENCE",
        event_time=pivot.available_at, recorded_at=pivot.available_at,
        decision_at=pivot.available_at,
        source_commit="c74fbd8e310ceaae9ed27f47837466eca9fece2b",
        config_hash=pivot.spec_id, rule_version=pivot.rule_version,
        variant_id="r1-offline", symbol=pivot.symbol, side=None,
        identities=IdentityRefs(), parent_event_ids=(),
        input_evidence_ids=(pivot.pivot_id,), input_evidence=(ref,),
        payload={"pivot_type": pivot.pivot_type.value,
                 "source_bar_id": pivot.source_bar_id,
                 "confirmation_bar_id": pivot.confirmation_bar_id,
                 "timeframe": pivot.timeframe,
                 "source_window_hash": pivot.source_window_hash},
    )
    assert envelope.execution_authority is False
    assert envelope.input_evidence[0].times == pivot.times
    assert envelope.input_evidence[0].data_hash == pivot.source_window_hash
    assert envelope.input_evidence_ids == (pivot.pivot_id,)


def test_break_support_hash_round_trips_into_r0_event_envelope():
    bars = series([10, 12, 11, 11, 13], [8, 9, 8.5, 9, 9],
                  ["9", "10", "10", "10", "13"])
    pivot = next(p for p in pivots(bars) if p.pivot_type is PivotType.HIGH
                 and p.source_bar_id == bars[1].bar_id)
    event = detect_structure_breaks(bars, (pivot,), spec(), as_of=bars[-1].available_at)[0]
    ref = InputEvidenceRef(event.break_id, "closed OHLCV and confirmed pivot",
                           event.times, event.break_support_hash)
    envelope = EventEnvelope(
        schema_version="1.0", event_type="V2_GENERIC_STRUCTURE_BREAK",
        event_time=event.available_at, recorded_at=event.available_at,
        decision_at=event.available_at,
        source_commit="c74fbd8e310ceaae9ed27f47837466eca9fece2b",
        config_hash=event.spec_id, rule_version=event.rule_version,
        variant_id="r1-offline", symbol=event.symbol, side=None,
        identities=IdentityRefs(), parent_event_ids=(),
        input_evidence_ids=(event.break_id,), input_evidence=(ref,),
        payload={"break_support_hash": event.break_support_hash},
    )
    assert envelope.execution_authority is False
    assert envelope.input_evidence[0].data_hash == event.break_support_hash


def test_pivot_source_window_hash_binds_all_supporting_bar_content():
    baseline = next(p for p in pivots(UP) if p.pivot_type is PivotType.HIGH
                    and p.source_bar_id == UP[1].bar_id)
    changed = (replace(UP[0], high=D("9.5")), *UP[1:])
    other = next(p for p in pivots(changed) if p.pivot_type is PivotType.HIGH
                 and p.source_bar_id == UP[1].bar_id)
    assert baseline.source_bar_id == other.source_bar_id
    assert baseline.confirmation_bar_id == other.confirmation_bar_id
    assert baseline.price == other.price and baseline.times == other.times
    assert baseline.source_window_hash != other.source_window_hash
    assert baseline.pivot_id != other.pivot_id
    assert pivots(UP) == pivots(tuple(UP))
    assert baseline.source_window_hash == canonical_sha256(UP[:3])


def test_pivot_id_rejects_mutated_stored_evidence():
    pivot = pivots(UP)[0]
    assert replace(pivot) == pivot
    future_times = CausalTimes(pivot.source_at, pivot.confirmed_at,
                               pivot.available_at + timedelta(seconds=1))
    alternate_hash = ("0" if pivot.source_window_hash[0] != "0" else "1") + pivot.source_window_hash[1:]
    for change in (dict(price=pivot.price + D("0.1")),
                   dict(times=future_times),
                   dict(source_window_hash=alternate_hash),
                   dict(symbol="OTHERUSDT")):
        with pytest.raises(ValueError, match="pivot_id"):
            replace(pivot, **change)


def test_structure_point_id_rejects_mutated_stored_evidence():
    point = classify_structure(pivots(UP), spec(), as_of=UP[-1].available_at)[0]
    assert replace(point) == point
    alternate_label = (StructureLabel.HH if point.label is not StructureLabel.HH
                       else StructureLabel.LH)
    future_times = CausalTimes(point.times.source_at, point.times.confirmed_at,
                               point.available_at + timedelta(seconds=1))
    for change in (dict(label=alternate_label),
                   dict(price=point.price + D("0.1")),
                   dict(times=future_times),
                   dict(symbol="OTHERUSDT")):
        with pytest.raises(ValueError, match="point_id"):
            replace(point, **change)


def test_break_support_hash_binds_previous_bar_content_without_changing_pivot():
    bars = series([10, 12, 11, 11, 13], [8, 9, 8.5, 9, 9],
                  ["9", "10", "10", "10", "13"])
    pivot = next(p for p in pivots(bars) if p.pivot_type is PivotType.HIGH
                 and p.source_bar_id == bars[1].bar_id)
    original = detect_structure_breaks(bars, (pivot,), spec(), as_of=bars[-1].available_at)
    altered = (*bars[:3], replace(bars[3], open=D("9.5"), close=D("9.5")), bars[4])
    same_pivot = next(p for p in pivots(altered) if p.pivot_type is PivotType.HIGH
                      and p.source_bar_id == bars[1].bar_id)
    changed = detect_structure_breaks(altered, (same_pivot,), spec(),
                                      as_of=altered[-1].available_at)
    assert same_pivot == pivot
    assert len(original) == len(changed) == 1
    assert original[0].break_support_hash != changed[0].break_support_hash
    assert original[0].break_id != changed[0].break_id
    assert original == detect_structure_breaks(bars, (pivot,), spec(),
                                               as_of=bars[-1].available_at)


def test_break_id_rejects_mutated_stored_evidence():
    bars = series([10, 12, 11, 11, 13], [8, 9, 8.5, 9, 9],
                  ["9", "10", "10", "10", "13"])
    pivot = next(p for p in pivots(bars) if p.pivot_type is PivotType.HIGH
                 and p.source_bar_id == bars[1].bar_id)
    event = detect_structure_breaks(bars, (pivot,), spec(), as_of=bars[-1].available_at)[0]
    assert replace(event) == event
    future_times = CausalTimes(event.times.source_at, event.times.confirmed_at,
                               event.available_at + timedelta(seconds=1))
    alternate_hash = ("0" if event.break_support_hash[0] != "0" else "1") + event.break_support_hash[1:]
    for change in (dict(price=event.price + D("0.1")),
                   dict(times=future_times),
                   dict(break_support_hash=alternate_hash),
                   dict(threshold=event.threshold + D("0.1"))):
        with pytest.raises(ValueError, match="break_id"):
            replace(event, **change)


def test_uptrend_downtrend_and_range_labels_use_prior_confirmed_same_type():
    up = classify_structure(pivots(UP), spec(), as_of=UP[-1].available_at)
    down = classify_structure(pivots(DOWN), spec(), as_of=DOWN[-1].available_at)
    assert StructureLabel.HH in {p.label for p in up}
    assert StructureLabel.HL in {p.label for p in up}
    assert StructureLabel.LH in {p.label for p in down}
    assert StructureLabel.LL in {p.label for p in down}
    range_points = classify_structure(pivots(RANGE), spec(), as_of=RANGE[-1].available_at)
    assert StructureLabel.EQUAL_HIGH in {p.label for p in range_points}
    assert StructureLabel.EQUAL_LOW in {p.label for p in range_points}
    for points in (up, down, range_points):
        for item in points:
            assert item.pivot_id in item.parent_evidence_ids
            if item.prior_pivot_id:
                assert item.prior_pivot_id in item.parent_evidence_ids


def test_equal_high_low_ties_choose_earliest_with_explicit_tolerance():
    bars = series([10, 12, 12.05, 10], [8, 6, 6.05, 8])
    found = pivots(bars, spec(tolerance="0.1"))
    assert [(p.pivot_type, p.source_bar_id) for p in found] == [
        (PivotType.HIGH, bars[1].bar_id), (PivotType.LOW, bars[1].bar_id),
    ]
    assert pivots(bars, spec(tolerance="0")) != found


def test_candidate_invalidated_before_right_confirmation_and_tiny_data():
    bars = series([10, 12, 13, 10], [8, 9, 10, 8])
    assert not any(p.source_bar_id == bars[1].bar_id and p.pivot_type is PivotType.HIGH
                   for p in pivots(bars))
    assert detect_confirmed_pivots(bars[:1], spec(), as_of=bars[0].available_at) == ()
    assert detect_confirmed_pivots((), spec(), as_of=BASE) == ()


def test_wick_only_does_not_break_but_closed_cross_does_in_both_directions():
    up = series([10, 12, 11, 13, 13, 14], [8, 9, 8.5, 9, 9, 9],
                ["9", "10", "10", "11", "11", "13"])
    high = next(p for p in pivots(up) if p.pivot_type is PivotType.HIGH and p.source_bar_id == up[1].bar_id)
    events = detect_structure_breaks(up, (high,), spec(), as_of=up[-1].available_at)
    assert len(events) == 1 and events[0].break_bar_id == up[5].bar_id
    assert events[0].break_direction is BreakDirection.UP
    assert events[0].break_mode is BreakMode.CLOSE_ONLY
    assert events[0].broken_level_id == high.pivot_id
    down = series([20, 21, 20, 20, 20, 20], [18, 17, 18, 16, 15, 14],
                  ["19", "19", "19", "18", "18", "16"])
    low = next(p for p in pivots(down) if p.pivot_type is PivotType.LOW and p.source_bar_id == down[1].bar_id)
    down_events = detect_structure_breaks(down, (low,), spec(), as_of=down[-1].available_at)
    assert len(down_events) == 1 and down_events[0].break_bar_id == down[5].bar_id
    assert down_events[0].break_direction is BreakDirection.DOWN


def test_explicit_buffer_and_no_future_pivot_usage():
    bars = series([10, 12, 11, 12.1, 13], [8, 9, 8.5, 9, 10],
                  ["9", "10", "10", "12.05", "12.2"])
    config = spec(buffer="0.3")
    high = next(p for p in pivots(bars, config) if p.pivot_type is PivotType.HIGH)
    assert detect_structure_breaks(bars, (high,), config,
                                   as_of=bars[-1].available_at) == ()
    earlier = bars[:2]
    with pytest.raises(ValueError, match="future unavailable pivot"):
        detect_structure_breaks(earlier, (high,), config, as_of=earlier[-1].available_at)


@pytest.mark.parametrize("direction", [BreakDirection.UP, BreakDirection.DOWN])
@pytest.mark.parametrize("delay_seconds", [0, 30])
def test_prior_confirmed_pivot_can_break_when_available_by_break_decision(
        direction, delay_seconds):
    if direction is BreakDirection.UP:
        bars = series([10, 12, 11, 13], [8, 9, 8.5, 9],
                      ["9", "10", "10", "13"])
        kind = PivotType.HIGH
    else:
        bars = series([20, 21, 20, 20], [18, 17, 18, 16],
                      ["19", "19", "19", "16"])
        kind = PivotType.LOW
    bars = (*bars[:2], replace(bars[2],
                              available_at=bars[2].available_at +
                              timedelta(seconds=delay_seconds)), bars[3])
    if delay_seconds == 0:
        bars = (*bars[:3], replace(bars[3],
                                   open_time=bars[3].open_time + timedelta(seconds=1)))
    pivot = next(p for p in pivots(bars) if p.pivot_type is kind
                 and p.source_bar_id == bars[1].bar_id)
    assert pivot.confirmed_at < bars[3].close_time
    assert pivot.available_at <= bars[3].available_at
    if delay_seconds:
        assert bars[3].open_time < pivot.available_at < bars[3].close_time
    else:
        assert pivot.available_at < bars[3].open_time
    events = detect_structure_breaks(bars, (pivot,), spec(),
                                     as_of=bars[-1].available_at)
    assert len(events) == 1
    assert events[0].break_direction is direction
    assert events[0].break_bar_id == bars[3].bar_id


def test_pivot_arriving_after_break_decision_cannot_break_retroactively():
    bars = series([10, 12, 11, 13], [8, 9, 8.5, 9],
                  ["9", "10", "10", "13"])
    pivot = next(p for p in pivots(bars) if p.pivot_type is PivotType.HIGH
                 and p.source_bar_id == bars[1].bar_id)
    late = reidentify_pivot(
        pivot, times=CausalTimes(pivot.source_at, pivot.confirmed_at,
                                 bars[-1].available_at + timedelta(seconds=1)))
    assert detect_structure_breaks(bars, (late,), spec(),
                                   as_of=late.available_at) == ()


def test_pivot_cannot_self_confirm_and_break_on_same_bar():
    bars = series([10, 12, 11, 13], [8, 9, 8.5, 9],
                  ["9", "10", "10", "13"])
    pivot = next(p for p in pivots(bars) if p.pivot_type is PivotType.HIGH
                 and p.source_bar_id == bars[1].bar_id)
    circular = reidentify_pivot(
        pivot, confirmation_bar_id=bars[3].bar_id,
        parent_evidence_ids=(*pivot.parent_evidence_ids, bars[3].bar_id),
        times=CausalTimes(pivot.source_at, bars[3].close_time,
                          bars[3].available_at))
    assert detect_structure_breaks(bars, (circular,), spec(),
                                   as_of=bars[-1].available_at) == ()


def test_delayed_feed_prefix_equivalence_including_intrabar_break():
    bars = series([10, 12, 11, 13, 11], [8, 9, 8.5, 9, 8.5],
                  ["9", "10", "10", "13", "10"])
    bars = (*bars[:2], replace(bars[2],
                              available_at=bars[2].available_at + timedelta(seconds=30)),
            *bars[3:])
    config = spec()
    all_pivots = pivots(bars)
    all_points = classify_structure(all_pivots, config, as_of=bars[-1].available_at)
    all_breaks = detect_structure_breaks(bars, all_pivots, config,
                                        as_of=bars[-1].available_at)
    assert any(event.break_bar_id == bars[3].bar_id for event in all_breaks)
    for end in range(1, len(bars) + 1):
        prefix = bars[:end]
        at = prefix[-1].available_at
        partial_pivots = detect_confirmed_pivots(prefix, config, as_of=at)
        assert partial_pivots == tuple(p for p in all_pivots if p.available_at <= at)
        assert classify_structure(partial_pivots, config, as_of=at) == tuple(
            p for p in all_points if p.available_at <= at)
        assert detect_structure_breaks(prefix, partial_pivots, config, as_of=at) == tuple(
            event for event in all_breaks if event.available_at <= at)


@pytest.mark.parametrize("bars", [UP, DOWN, RANGE])
def test_prefix_equivalence_no_repainting_and_deterministic_ids(bars):
    config = spec()
    full_pivots = pivots(bars)
    full_points = classify_structure(full_pivots, config, as_of=bars[-1].available_at)
    full_breaks = detect_structure_breaks(bars, full_pivots, config, as_of=bars[-1].available_at)
    for end in range(1, len(bars) + 1):
        prefix = bars[:end]
        at = prefix[-1].available_at
        partial_pivots = detect_confirmed_pivots(prefix, config, as_of=at)
        partial_points = classify_structure(partial_pivots, config, as_of=at)
        partial_breaks = detect_structure_breaks(prefix, partial_pivots, config, as_of=at)
        assert partial_pivots == tuple(item for item in full_pivots if item.available_at <= at)
        assert partial_points == tuple(item for item in full_points if item.available_at <= at)
        assert partial_breaks == tuple(item for item in full_breaks if item.available_at <= at)
    assert pivots(bars) == full_pivots
    assert canonical_sha256(full_points) == canonical_sha256(classify_structure(pivots(bars), config,
                                                                                as_of=bars[-1].available_at))


def test_up_down_mirror_preserves_pivots_and_structure_labels():
    ceiling = D("30")
    mirrored = tuple(replace(item, open=ceiling - item.open,
                             close=ceiling - item.close,
                             high=ceiling - item.low,
                             low=ceiling - item.high)
                     for item in UP)
    original_pivots = pivots(UP)
    mirrored_pivots = pivots(mirrored)
    assert len(original_pivots) == len(mirrored_pivots)
    mirror_by_source_type = {(item.source_bar_id, item.pivot_type): item
                             for item in mirrored_pivots}
    for original in original_pivots:
        reflected_type = (PivotType.LOW if original.pivot_type is PivotType.HIGH
                          else PivotType.HIGH)
        other = mirror_by_source_type[(original.source_bar_id, reflected_type)]
        assert original.price + other.price == ceiling
        assert original.times == other.times
    labels = classify_structure(original_pivots, spec(), as_of=UP[-1].available_at)
    mirror_labels = classify_structure(mirrored_pivots, spec(), as_of=UP[-1].available_at)
    mirror_label_by_pivot = {item.pivot_id: item.label for item in mirror_labels}
    reflected = {StructureLabel.HH: StructureLabel.LL,
                 StructureLabel.HL: StructureLabel.LH,
                 StructureLabel.LH: StructureLabel.HL,
                 StructureLabel.LL: StructureLabel.HH,
                 StructureLabel.UNCLASSIFIED: StructureLabel.UNCLASSIFIED}
    for item in labels:
        original = next(pivot for pivot in original_pivots if pivot.pivot_id == item.pivot_id)
        reflected_type = (PivotType.LOW if original.pivot_type is PivotType.HIGH
                          else PivotType.HIGH)
        other = mirror_by_source_type[(original.source_bar_id, reflected_type)]
        assert mirror_label_by_pivot[other.pivot_id] is reflected[item.label]


def test_r1_package_imports_and_calls_have_no_runtime_authority():
    package = Path(__file__).resolve().parents[1] / "strategy_v2"
    forbidden = ("core", "shadow_runtime", "requests", "httpx", "aiohttp",
                 "socket", "sqlite3", "telegram", "binance", "ccxt", "railway")
    for name in ("market_data.py", "structure_models.py", "structure.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not item.name.lower().startswith(forbidden) for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert not (node.module or "").lower().startswith(forbidden)
            elif isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr in
                            ("now", "utcnow", "connect", "create_order", "write_text", "write_bytes"))


def test_future_unavailable_bars_and_pivots_fail_closed():
    with pytest.raises(ValueError, match="future unavailable bar"):
        detect_confirmed_pivots(UP, spec(), as_of=UP[2].available_at)
    full = pivots(UP)
    with pytest.raises(ValueError, match="future unavailable pivot"):
        classify_structure(full, spec(), as_of=UP[2].available_at)


def test_protected_level_remains_typed_unresolved_without_thesis():
    result = derive_protected_levels(pivots(UP), spec(), as_of=UP[-1].available_at)
    assert result.status is ProtectionStatus.UNRESOLVED
    assert result.levels == ()
    assert "THESIS" in result.reason


def test_protected_level_schema_requires_thesis_continuation_and_explicit_reason():
    pivot = pivots(UP)[0]
    level = ProtectedLevel("level-1", pivot.pivot_type, pivot.price,
                           pivot.pivot_id, ThesisId("thesis-1"), "continuation-1",
                           "prior confirmed pivot survived documented continuation",
                           pivot.symbol, pivot.timeframe, pivot.times,
                           (pivot.pivot_id, "continuation-1"),
                           pivot.spec_id, pivot.rule_version)
    assert level.originating_pivot_id == pivot.pivot_id
    assert level.thesis_id == ThesisId("thesis-1")
    with pytest.raises(TypeError, match="ThesisId"):
        replace(level, thesis_id="thesis-1")
    with pytest.raises(ValueError, match="reason"):
        replace(level, protection_reason="")


@pytest.mark.parametrize("change", [
    {"pivot_radius_left": 0}, {"pivot_radius_right": True},
    {"equal_price_tolerance": D("NaN")}, {"equal_price_tolerance": 0.1},
    {"close_break_buffer": D("-1")}, {"rule_version": ""},
])
def test_spec_needs_explicit_valid_research_parameters(change):
    with pytest.raises((ValueError, TypeError)):
        replace(spec(), **change)


def test_no_execution_or_runtime_authority_on_r1_outputs():
    found = pivots(UP)
    point = classify_structure(found, spec(), as_of=UP[-1].available_at)[0]
    for item in (*found, point):
        assert not hasattr(item, "execution_authority")
        assert not any(hasattr(item, name) for name in
                       ("entry", "stop", "target", "order_id", "position_size"))
