"""R2A acceptance: causal numeric candidates never become authoritative BOS."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, Inexact, Overflow, Rounded, localcontext
from pathlib import Path

import pytest

from strategy_v2.events import canonical_sha256
from strategy_v2.market_data import ClosedBar
from strategy_v2.r2a_models import (
    ATRSourcePolicy, BOSInputSpec, EvidenceResolutionReason as Reason,
    GridPolicy, InstrumentTickObservation, NumericPolicy, ThresholdStatus,
    VolatilityObservation,
)
from strategy_v2.r2a_threshold import R2AContractError, evaluate_close_break_candidate
from strategy_v2.structure import detect_confirmed_pivots, detect_structure_breaks
from strategy_v2.structure_models import EqualTiePolicy, PivotType, StructureSpec


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
COMMIT = "9d365aa9e1a05f514555f33ba8d7e0e1f63cfdc1"


def bar(index, high, low, close, *, available_delay=0):
    start = BASE + timedelta(minutes=index)
    return ClosedBar(
        f"r2a-bar-{index}", "ABCUSDT", "1m", start, start + timedelta(minutes=1),
        start + timedelta(minutes=1, seconds=available_delay),
        D(close), D(high), D(low), D(close), D("5"), True,
    )


def source_bars(*, close="14", delay=0):
    return (
        bar(0, "10", "8", "9"),
        bar(1, "12", "9", "10"),
        bar(2, "11", "8.5", "10"),
        bar(3, "11", "9", "10"),
        bar(4, max(D("13"), D(close)), "9", close, available_delay=delay),
    )


def fixture(*, close="14", atr_policy=ATRSourcePolicy.PRE_BREAK_CLOSED,
            b="1", atr_value="1", available_delay=0):
    bars = source_bars(close=close)
    structure_spec = StructureSpec(1, 1, D("0"), EqualTiePolicy.EARLIEST,
                                   D("0.5"), "r1")
    pivot = next(p for p in detect_confirmed_pivots(bars[:3], structure_spec,
                                                    as_of=bars[2].available_at)
                 if p.pivot_type is PivotType.HIGH)
    atr_window = (bars[2:4] if atr_policy is ATRSourcePolicy.PRE_BREAK_CLOSED
                  else bars[3:5])
    atr = VolatilityObservation(
        D(atr_value), 2, "EXTERNAL_ATR_OBSERVATION", "ABCUSDT", "1m",
        "TRADE_OHLCV", canonical_sha256(atr_window), atr_window[-1].close_time,
        atr_window[-1].close_time,
        atr_window[-1].available_at + timedelta(seconds=available_delay),
        "atr-observation-v1",
    )
    metadata = tuple(sorted((
        ("price_basis", "TRADE_OHLCV"), ("symbol", "ABCUSDT"),
        ("tick_size", "0.1"), ("tick_floor", "0.1"),
    )))
    tick = InstrumentTickObservation(
        "ABCUSDT", "TRADE_OHLCV", D("0.1"), D("0.1"), "frozen-filter-1",
        BASE, BASE, metadata, canonical_sha256(metadata),
    )
    spec = BOSInputSpec(
        source_commit=COMMIT, structure_spec_hash=canonical_sha256(structure_spec),
        rule_version="r2a", variant_id="explicit-variant-1",
        threshold_policy_id="threshold-policy-1", multiplier_b=D(b),
        atr_source_policy=atr_policy, atr_method_id=atr.method,
        atr_period=atr.period, atr_policy_id=atr.policy_id,
        grid_policy=GridPolicy.OUTWARD_TICK,
        numeric_policy=NumericPolicy.EXACT_FRACTION_BOUNDED_V1,
        price_basis="TRADE_OHLCV",
        symbol="ABCUSDT", timeframe="1m",
    )
    params = dict(
        bars=bars[3:5], pivot=pivot, pivot_window=bars[:3],
        structure_spec=structure_spec, atr=atr, atr_window=atr_window,
        tick=tick, spec=spec, decision_at=max(bars[4].available_at,
                                              atr.available_at),
    )
    return params


def evaluate(**changes):
    params = fixture()
    params.update(changes)
    return evaluate_close_break_candidate(**params)


def test_close_cross_pass_is_not_bos_or_trading_authority():
    candidate = evaluate()
    assert candidate.status is ThresholdStatus.THRESHOLD_PASSED
    assert candidate.threshold_delta == D("1")
    assert candidate.threshold_price == D("13")
    assert candidate.reason is None
    assert candidate.qualification.status is ThresholdStatus.UNRESOLVED
    assert candidate.qualification.reason is Reason.STRUCTURE_ROLE_NOT_PROVEN
    assert candidate.execution_authority is False
    assert candidate.event_id == evaluate().event_id
    assert candidate.source_commit == COMMIT
    assert candidate.variant_hash == fixture()["spec"].variant_hash
    assert candidate.threshold_policy_id == "threshold-policy-1"
    assert candidate.parent_evidence_ids == (
        fixture()["pivot"].pivot_id, fixture()["bars"][0].bar_id,
        fixture()["bars"][1].bar_id, fixture()["atr"].evidence_id,
        fixture()["tick"].evidence_id,
    )
    assert candidate.source_at <= candidate.confirmed_at <= candidate.available_at <= candidate.decision_at


@pytest.mark.parametrize("close", ["13", "12.9", "10"])
def test_exact_boundary_or_wick_only_does_not_pass(close):
    params = fixture(close=close)
    candidate = evaluate_close_break_candidate(**params)
    assert candidate.status is ThresholdStatus.THRESHOLD_NOT_PASSED
    assert candidate.reason is Reason.NO_CLOSE_CROSS


def test_explicit_pre_break_and_break_close_atr_are_separate_variants():
    pre = fixture(atr_policy=ATRSourcePolicy.PRE_BREAK_CLOSED)
    at_close = fixture(atr_policy=ATRSourcePolicy.BREAK_CLOSE)
    a = evaluate_close_break_candidate(**pre)
    b = evaluate_close_break_candidate(**at_close)
    assert a.status is b.status is ThresholdStatus.THRESHOLD_PASSED
    assert a.event_id != b.event_id
    assert pre["spec"].variant_hash != at_close["spec"].variant_hash
    assert evaluate_close_break_candidate(**{**pre, "spec": at_close["spec"]}).reason is Reason.ATR_WINDOW_POLICY_MISMATCH


def test_generic_r1_break_and_r2a_threshold_can_disagree():
    params = fixture(close="12.8")
    generic = detect_structure_breaks(source_bars(close="12.8"),
                                      (params["pivot"],), params["structure_spec"],
                                      as_of=params["decision_at"])
    assert any(item.broken_level_id == params["pivot"].pivot_id for item in generic)
    assert evaluate_close_break_candidate(**params).status is ThresholdStatus.THRESHOLD_NOT_PASSED


def test_pivot_source_content_is_resolved_not_just_digest_shape():
    params = fixture()
    changed = (replace(params["pivot_window"][0], high=D("10.1")),
               *params["pivot_window"][1:])
    candidate = evaluate_close_break_candidate(**{**params, "pivot_window": changed})
    assert candidate.status is ThresholdStatus.UNRESOLVED
    assert candidate.reason is Reason.PIVOT_SOURCE_MISMATCH


def test_atr_source_content_and_policy_end_are_verified():
    params = fixture()
    changed = (replace(params["atr_window"][0], low=D("8.4")),
               params["atr_window"][1])
    assert evaluate_close_break_candidate(**{**params, "atr_window": changed}).reason is Reason.ATR_SOURCE_MISMATCH
    stale = params["atr_window"][:-1]
    assert evaluate_close_break_candidate(**{**params, "atr_window": stale}).reason is Reason.ATR_SOURCE_MISMATCH


def test_late_feed_prevents_backdating_then_allows_delayed_decision():
    params = fixture(available_delay=100)
    early = params["bars"][-1].available_at
    assert evaluate_close_break_candidate(**{**params, "decision_at": early}).reason is Reason.FUTURE_EVIDENCE
    late = evaluate_close_break_candidate(**params)
    assert late.status is ThresholdStatus.THRESHOLD_PASSED
    assert late.decision_at == params["atr"].available_at
    assert late.available_at == late.decision_at


def test_pivot_arriving_during_break_bar_is_not_backdated():
    params = fixture()
    all_bars = source_bars()
    delayed2 = replace(all_bars[2], available_at=all_bars[2].available_at + timedelta(seconds=70))
    delayed3 = replace(all_bars[3], available_at=all_bars[3].available_at + timedelta(seconds=20))
    window = (all_bars[0], all_bars[1], delayed2)
    pivot = next(p for p in detect_confirmed_pivots(window, params["structure_spec"],
                                                     as_of=delayed2.available_at)
                 if p.pivot_type is PivotType.HIGH)
    atr_window = (delayed2, delayed3)
    atr = replace(params["atr"], source_bars_hash=canonical_sha256(atr_window),
                  available_at=delayed3.available_at)
    result = evaluate_close_break_candidate(**{**params, "pivot": pivot,
                                               "pivot_window": window,
                                               "atr": atr, "atr_window": atr_window,
                                               "bars": (delayed3, all_bars[4])})
    assert result.status is ThresholdStatus.THRESHOLD_PASSED
    assert all_bars[4].open_time < pivot.available_at < all_bars[4].close_time
    assert result.decision_at >= all_bars[4].close_time


@pytest.mark.parametrize("missing,reason", [
    ("spec", Reason.MISSING_SPEC), ("pivot", Reason.MISSING_PIVOT),
    ("atr", Reason.MISSING_ATR), ("tick", Reason.MISSING_TICK),
    ("pivot_window", Reason.MISSING_SOURCE_BARS),
])
def test_missing_inputs_fail_closed_with_typed_reason(missing, reason):
    params = fixture()
    params[missing] = None
    candidate = evaluate_close_break_candidate(**params)
    assert candidate.status is ThresholdStatus.UNRESOLVED
    assert candidate.reason is reason


def test_off_grid_and_nonpositive_short_threshold_fail_closed():
    params = fixture()
    bad = replace(params["bars"][1], close=D("14.05"), high=D("14.05"),
                  open=D("14.05"))
    assert evaluate_close_break_candidate(**{**params, "bars": (params["bars"][0], bad)}).reason is Reason.OFF_GRID_PRICE
    # A low pivot can have a buffer larger than its positive price.
    spec = replace(params["spec"], multiplier_b=D("100"))
    mirrored = tuple(replace(item, open=D("30") - item.open,
                             high=D("30") - item.low,
                             low=D("30") - item.high,
                             close=D("30") - item.close)
                     for item in source_bars())
    low_pivot = next(p for p in detect_confirmed_pivots(mirrored[:3],
                                                        params["structure_spec"],
                                                        as_of=mirrored[2].available_at)
                     if p.pivot_type is PivotType.LOW)
    low_window = mirrored[:3]
    atr_window = mirrored[2:4]
    atr = replace(params["atr"], source_bars_hash=canonical_sha256(atr_window))
    assert evaluate_close_break_candidate(**{**params, "pivot": low_pivot,
                                            "pivot_window": low_window,
                                            "bars": mirrored[3:5], "atr_window": atr_window,
                                            "atr": atr, "spec": spec}).reason is Reason.NONPOSITIVE_THRESHOLD


def test_tick_floor_grid_and_explicit_multiplier_validation():
    params = fixture(b="0.15", atr_value="1")
    candidate = evaluate_close_break_candidate(**params)
    assert candidate.threshold_delta == D("0.15")
    assert candidate.threshold_price == D("12.2")
    assert candidate.status is ThresholdStatus.THRESHOLD_PASSED
    with pytest.raises(ValueError):
        replace(params["spec"], multiplier_b=D("NaN"))
    with pytest.raises(ValueError):
        replace(params["tick"], tick_floor=D("0.15"))
    with pytest.raises(ValueError):
        replace(params["atr"], atr=D("0"))


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_numeric_threshold_is_independent_of_ambient_decimal_precision(side):
    params = fixture(close="13.1", b="1.000000000001")
    if side == "SHORT":
        original = source_bars(close="13.1")
        mirrored = tuple(replace(item, open=D("30") - item.open,
                                 high=D("30") - item.low,
                                 low=D("30") - item.high,
                                 close=D("30") - item.close)
                         for item in original)
        low_pivot = next(p for p in detect_confirmed_pivots(
            mirrored[:3], params["structure_spec"], as_of=mirrored[2].available_at)
            if p.pivot_type is PivotType.LOW)
        atr_window = mirrored[2:4]
        params.update(bars=mirrored[3:5], pivot=low_pivot,
                      pivot_window=mirrored[:3], atr_window=atr_window,
                      atr=replace(params["atr"], source_bars_hash=canonical_sha256(atr_window)))
    observed = []
    for precision in (9, 12, 28, 50):
        with localcontext() as context:
            context.prec = precision
            context.Emax = 9
            context.Emin = -9
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            context.traps[Overflow] = True
            candidate = evaluate_close_break_candidate(**params)
        observed.append((candidate.status, candidate.reason, candidate.threshold_delta,
                         candidate.threshold_price, candidate.event_id, candidate.variant_hash))
    assert len(set(observed)) == 1
    status, reason, delta, threshold, _, _ = observed[0]
    assert status is ThresholdStatus.THRESHOLD_NOT_PASSED
    assert reason is Reason.NO_CLOSE_CROSS
    assert delta == D("1.000000000001")
    assert threshold == (D("13.1") if side == "LONG" else D("16.9"))


@pytest.mark.parametrize("field,value", [
    ("multiplier_b", "1E+1000000"), ("multiplier_b", "1E-1000000"),
    ("atr", "1E+1000000"), ("atr", "1E-1000000"),
])
def test_extreme_arithmetic_inputs_return_typed_unresolved(field, value):
    params = fixture()
    if field == "multiplier_b":
        params["spec"] = replace(params["spec"], multiplier_b=D(value))
    else:
        params["atr"] = replace(params["atr"], atr=D(value))
    for precision in (9, 28, 50):
        with localcontext() as context:
            context.prec = precision
            candidate = evaluate_close_break_candidate(**params)
        assert candidate.status is ThresholdStatus.UNRESOLVED
        assert candidate.reason is Reason.NUMERIC_THRESHOLD_UNCOMPUTABLE
        assert candidate.execution_authority is False


def test_extreme_tick_is_typed_unresolved_and_numeric_policy_is_explicit():
    params = fixture()
    tiny_tick = D("1E-1000000")
    metadata = tuple(sorted((("price_basis", "TRADE_OHLCV"), ("symbol", "ABCUSDT"),
                             ("tick_size", str(tiny_tick)), ("tick_floor", str(tiny_tick)))))
    params["tick"] = replace(params["tick"], tick_size=tiny_tick, tick_floor=tiny_tick,
                             metadata_fields=metadata, metadata_hash=canonical_sha256(metadata))
    candidate = evaluate_close_break_candidate(**params)
    assert candidate.status is ThresholdStatus.UNRESOLVED
    assert candidate.reason is Reason.NUMERIC_THRESHOLD_UNCOMPUTABLE
    with pytest.raises(TypeError, match="numeric policy"):
        replace(params["spec"], numeric_policy="implicit-context")
    assert params["spec"].numeric_policy.value in params["spec"].numeric_policy.name


@pytest.mark.parametrize("close", ["12.0", "12.1", "12.2", "12.3", "13.0", "13.1", "14.0"])
@pytest.mark.parametrize("b", ["0", "0.15", "1", "2"])
def test_long_short_mirror_property_on_tick_grid(close, b):
    params = fixture(close=close, b=b)
    long_result = evaluate_close_break_candidate(**params)
    original = source_bars(close=close)
    mirrored = tuple(replace(item, open=D("30") - item.open,
                             high=D("30") - item.low,
                             low=D("30") - item.high,
                             close=D("30") - item.close)
                     for item in original)
    low_pivot = next(p for p in detect_confirmed_pivots(
        mirrored[:3], params["structure_spec"], as_of=mirrored[2].available_at)
        if p.pivot_type is PivotType.LOW)
    atr_window = mirrored[2:4]
    short_params = {**params, "bars": mirrored[3:5], "pivot": low_pivot,
                    "pivot_window": mirrored[:3], "atr_window": atr_window,
                    "atr": replace(params["atr"],
                                   source_bars_hash=canonical_sha256(atr_window))}
    short_result = evaluate_close_break_candidate(**short_params)
    assert short_result.status is long_result.status
    assert short_result.threshold_price == D("30") - long_result.threshold_price
    assert short_result.threshold_delta == long_result.threshold_delta
    assert short_result.execution_authority is long_result.execution_authority is False


def test_mutated_metadata_and_mixed_context_fail_closed():
    params = fixture()
    with pytest.raises(ValueError, match="metadata_hash"):
        replace(params["tick"], metadata_hash="0" * 64)
    with pytest.raises(ValueError, match="bind"):
        replace(params["tick"], tick_size=D("0.05"))
    assert evaluate_close_break_candidate(**{
        **params, "spec": replace(params["spec"], symbol="OTHERUSDT")
    }).reason is Reason.MIXED_CONTEXT
    assert evaluate_close_break_candidate(**{
        **params, "structure_spec": replace(params["structure_spec"],
                                             close_break_buffer=D("0.6"))
    }).reason is Reason.SPEC_MISMATCH


def test_no_future_evidence_or_unexplained_bar_gap():
    params = fixture()
    early = params["bars"][-1].close_time - timedelta(seconds=1)
    assert evaluate_close_break_candidate(**{**params, "decision_at": early}).reason is Reason.FUTURE_EVIDENCE
    gap = replace(params["bars"][1], open_time=params["bars"][1].open_time + timedelta(seconds=1))
    assert evaluate_close_break_candidate(**{
        **params, "bars": (params["bars"][0], gap)
    }).reason is Reason.INVALID_BAR_SEQUENCE


def test_duplicate_evidence_and_wrong_input_type_fail_closed():
    params = fixture()
    duplicate = replace(params["bars"][1], bar_id=params["bars"][0].bar_id)
    assert evaluate_close_break_candidate(**{
        **params, "bars": (params["bars"][0], duplicate)
    }).reason is Reason.DUPLICATE_EVIDENCE
    assert evaluate_close_break_candidate(**{
        **params, "bars": ("not-a-bar", params["bars"][1])
    }).reason is Reason.INVALID_EVIDENCE_STRUCTURE
    assert evaluate_close_break_candidate(**{
        **params, "pivot": "not-a-pivot"
    }).reason is Reason.INVALID_EVIDENCE_TYPE


@pytest.mark.parametrize("name,value,reason", [
    ("bars", [1, 2], Reason.INVALID_EVIDENCE_TYPE),
    ("bars", ("corrupt",), Reason.INVALID_EVIDENCE_STRUCTURE),
    ("pivot_window", ("corrupt",), Reason.INVALID_EVIDENCE_STRUCTURE),
    ("atr_window", ("corrupt",), Reason.INVALID_EVIDENCE_STRUCTURE),
    ("atr_window", "bad", Reason.INVALID_EVIDENCE_TYPE),
    ("pivot", object(), Reason.INVALID_EVIDENCE_TYPE),
    ("structure_spec", object(), Reason.INVALID_EVIDENCE_TYPE),
    ("atr", object(), Reason.INVALID_EVIDENCE_TYPE),
    ("tick", object(), Reason.INVALID_EVIDENCE_TYPE),
    ("spec", object(), Reason.INVALID_EVIDENCE_TYPE),
])
def test_malformed_inputs_return_typed_unresolved(name, value, reason):
    params = fixture()
    params[name] = value
    result = evaluate_close_break_candidate(**params)
    assert result.status is ThresholdStatus.UNRESOLVED
    assert result.reason is reason
    assert result.execution_authority is False
    assert result.evidence_hash


@pytest.mark.parametrize("decision_at", [None, "bad", BASE.replace(tzinfo=None)])
def test_unanchored_decision_time_is_documented_contract_error(decision_at):
    params = fixture()
    with pytest.raises(R2AContractError, match="decision_at"):
        evaluate_close_break_candidate(**{**params, "decision_at": decision_at})


@pytest.mark.parametrize("field,value", [
    ("method", "OTHER_ATR_METHOD"), ("period", 1),
    ("policy_id", "other-policy"),
])
def test_atr_method_period_policy_are_frozen_in_variant(field, value):
    params = fixture()
    changed_atr = replace(params["atr"], **{field: value})
    candidate = evaluate_close_break_candidate(**{**params, "atr": changed_atr})
    assert candidate.status is ThresholdStatus.UNRESOLVED
    assert candidate.reason is Reason.ATR_VARIANT_MISMATCH
    spec_field = {"method": "atr_method_id", "period": "atr_period",
                  "policy_id": "atr_policy_id"}[field]
    separate_variant = replace(params["spec"], **{spec_field: value})
    assert separate_variant.variant_hash != params["spec"].variant_hash
    assert evaluate_close_break_candidate(**{
        **params, "atr": changed_atr, "spec": separate_variant
    }).status is ThresholdStatus.THRESHOLD_PASSED


def test_atr_value_is_market_observation_not_variant_policy():
    first = fixture(atr_value="1")
    second = fixture(atr_value="2")
    a = evaluate_close_break_candidate(**first)
    b = evaluate_close_break_candidate(**second)
    assert a.status is ThresholdStatus.THRESHOLD_PASSED
    assert b.status is ThresholdStatus.THRESHOLD_NOT_PASSED
    assert a.variant_hash == b.variant_hash
    assert a.atr_evidence_id != b.atr_evidence_id
    assert a.event_id != b.event_id


def test_mutation_changes_content_identity_and_is_prefix_stable():
    params = fixture()
    original = evaluate_close_break_candidate(**params)
    assert replace(original, threshold_price=D("13.1")).event_id != original.event_id
    assert replace(params["spec"], multiplier_b=D("2")).variant_hash != params["spec"].variant_hash
    extension = source_bars() + (bar(5, "15", "10", "11"),)
    before_extension = evaluate_close_break_candidate(**{**params, "bars": extension[3:5]})
    assert before_extension.event_id == original.event_id
    assert extension[-1].available_at > original.decision_at
    assert evaluate_close_break_candidate(**{
        **params, "bars": extension[3:5]
    }).event_id == before_extension.event_id
    # The new close may support a different observation, but cannot rewrite
    # the event already derived from the immutable earlier prefix.
    assert evaluate_close_break_candidate(**{
        **params, "bars": extension[4:6]
    }).status is ThresholdStatus.UNRESOLVED


def test_missing_or_future_source_timestamps_cannot_construct_evidence():
    params = fixture()
    with pytest.raises(ValueError):
        replace(params["atr"], available_at=None)
    with pytest.raises(ValueError):
        replace(params["tick"], effective_at=BASE + timedelta(days=1),
                available_at=BASE)
    future = replace(params["tick"], effective_at=BASE + timedelta(minutes=5),
                     available_at=BASE + timedelta(minutes=5))
    assert evaluate_close_break_candidate(**{**params, "tick": future,
                                            "decision_at": future.available_at}).reason is Reason.TICK_NOT_EFFECTIVE


def test_no_execution_imports_or_runtime_hooks():
    root = Path(__file__).parents[1]
    for name in ("r2a_models.py", "r2a_threshold.py"):
        tree = ast.parse((root / "strategy_v2" / name).read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)]
        assert not any(any(token in module for token in
                           ("exchange", "binance", "main", "telegram", "risk_manager"))
                       for module in imports)
