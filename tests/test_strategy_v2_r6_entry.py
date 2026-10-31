"""Pure R6 causal entry qualification tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from strategy_v2.causality import CausalTimes
from strategy_v2.enums import InvalidationRelation, InvalidationScope, Side
from strategy_v2.events import canonical_sha256
from strategy_v2.models import InvalidationLevel
from strategy_v2.r2b_models import (
    MarketSlotIdentity,
    R2BEvaluationFamily,
    R2BOutcome,
    R2BStructuralEvaluation,
)
from strategy_v2.r3_thesis import (
    ThesisPolicySpec,
    ThesisStatus,
    create_thesis,
    derive_thesis_id,
    transition_thesis,
)
from strategy_v2.r6_entry import (
    ClosedTriggerEvidence,
    EntryGateReason,
    EntryGateStatus,
    EntryPolicySpec,
    EntryZone,
    ExecutableQuote,
    RegimeContextSnapshot,
    RegimeLabel,
    TriggerType,
    VolatilityEvidence,
    qualify_entry,
)


D = Decimal
BASE = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
COMMIT = "2c9ba8c818370dc60472bd18ae4eb4566ee78169"
R2B_HASH = canonical_sha256(("r2b-entry-fixture",)).lower()
HTF_HASH = canonical_sha256(("4h", "1h", "context")).lower()


def waiting_thesis(side=Side.LONG, final_status=ThesisStatus.WAIT_TRIGGER):
    slot = MarketSlotIdentity(
        R2BEvaluationFamily.INITIAL_BREAK, "ABCUSDT", "15m", "epoch-1",
        "reference-1", "break-bar", side, "TRADE_OHLCV", R2B_HASH,
    )
    initial = R2BStructuralEvaluation(
        R2BOutcome.INITIAL_BREAK_CONFIRMED, None, slot, "role-1",
        ("source-1",), canonical_sha256((slot, "source")).lower(),
        BASE + timedelta(minutes=1),
    )
    policy = ThesisPolicySpec(
        COMMIT, "R3.1", "R3_OFFLINE_RESEARCH_V1", "CAUSAL_THESIS_V1",
        R2B_HASH, "THESIS_LIFECYCLE_V1", "POST_CLOSE_CAUSAL_RESET_V1",
    )
    thesis_id = derive_thesis_id(
        symbol="ABCUSDT", side=side, setup_family="BOS_RETEST",
        epoch_id="epoch-1", initial_break_event_id=initial.market_event_id,
        htf_context_snapshot_hash=HTF_HASH, causal_zone_ids=("zone-source",),
        policy=policy,
    )
    at = BASE + timedelta(minutes=2)
    invalidation = InvalidationLevel(
        "invalidation-1", InvalidationScope.THESIS,
        D("95") if side is Side.LONG else D("105"),
        InvalidationRelation.BREAK_BELOW if side is Side.LONG
        else InvalidationRelation.BREAK_ABOVE,
        side, "PROTECTED_STRUCTURE", "15m",
        CausalTimes(at - timedelta(minutes=1), at, at), thesis_id=thesis_id,
    )
    thesis = create_thesis(
        initial_break=initial, symbol="ABCUSDT", side=side,
        setup_family="BOS_RETEST", epoch_id="epoch-1",
        htf_context_snapshot_hash=HTF_HASH,
        causal_zone_ids=("zone-source",), invalidation=invalidation,
        policy=policy, decision_at=BASE + timedelta(minutes=3),
    )
    thesis = transition_thesis(
        thesis, target=ThesisStatus.QUALIFIED,
        event_at=BASE + timedelta(minutes=4),
        decision_at=BASE + timedelta(minutes=4), evidence_ids=("qualified",),
        reason_code="QUALIFIED",
    ).snapshot
    if final_status is ThesisStatus.QUALIFIED:
        return thesis
    return transition_thesis(
        thesis, target=ThesisStatus.WAIT_TRIGGER,
        event_at=BASE + timedelta(minutes=5),
        decision_at=BASE + timedelta(minutes=5), evidence_ids=("zone-ready",),
        reason_code="WAIT_TRIGGER",
    ).snapshot


def regime(side=Side.LONG, *, available=BASE + timedelta(minutes=6), label=None):
    label = label or (RegimeLabel.TREND_UP if side is Side.LONG else RegimeLabel.TREND_DOWN)
    return RegimeContextSnapshot(
        "ABCUSDT", "ABC-PERP-E1", label, ("4h", "1h"), available - timedelta(minutes=1),
        available, ("4h-structure", "1h-context"),
        canonical_sha256((side, label, "regime")).lower(),
    )


def zone(thesis, *, available=BASE + timedelta(minutes=7), expires=BASE + timedelta(minutes=30)):
    return EntryZone(
        "entry-zone-1", thesis.identity.thesis_id, "ABCUSDT",
        "ABC-PERP-E1", thesis.identity.side, "15m", D("99"), D("101"),
        available - timedelta(minutes=1), available, expires,
        ("zone-source", "zone-confirmation"),
    )


def trigger(thesis, *, available=BASE + timedelta(minutes=10), final=True,
            trigger_type=TriggerType.CLOSED_RETEST_REJECTION):
    return ClosedTriggerEvidence(
        "trigger-1", thesis.identity.thesis_id, "entry-zone-1", "ABCUSDT",
        "ABC-PERP-E1", thesis.identity.side, "5m", trigger_type, "5m-bar-1", D("100"),
        available - timedelta(seconds=1), available, final,
        canonical_sha256((thesis.identity.side, available, "trigger")).lower(),
    )


def volatility(*, available=BASE + timedelta(minutes=9)):
    return VolatilityEvidence(
        "ABCUSDT", "ABC-PERP-E1", "15m", D("2"), canonical_sha256(("atr-window",)).lower(),
        available - timedelta(minutes=1), available,
    )


def quote(side=Side.LONG, *, bid="100.0", ask="100.1",
          available=BASE + timedelta(minutes=10, seconds=1)):
    return ExecutableQuote(
        "ABCUSDT", "ABC-PERP-E1", D(bid), D(ask), available, available,
        canonical_sha256((side, bid, ask, available)).lower(),
    )


def policy(**overrides):
    values = dict(
        source_commit=COMMIT, policy_id="CAUSAL_ENTRY_V1", rule_version="r6",
        zone_timeframe="15m", trigger_timeframe="5m",
        allowed_trigger_types=(TriggerType.CLOSED_RETEST_REJECTION,),
        max_chase_atr_multiple=D("0.25"), max_spread_fraction=D("0.002"),
        max_quote_age_seconds=3, ratio_decimal_places=8,
        numeric_policy_id="EXACT_FRACTION_ENTRY_V1",
    )
    values.update(overrides)
    return EntryPolicySpec(**values)


def inputs(side=Side.LONG, **overrides):
    thesis = waiting_thesis(side)
    values = dict(
        thesis=thesis, regime=regime(side), zone=zone(thesis),
        trigger=trigger(thesis), volatility=volatility(), quote=quote(side),
        policy=policy(), decision_at=BASE + timedelta(minutes=10, seconds=2),
    )
    values.update(overrides)
    return values


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_long_short_causal_entry_is_symmetric(side):
    result = qualify_entry(**inputs(side))
    assert result.status is EntryGateStatus.READY
    assert result.executable_price == (D("100.1") if side is Side.LONG else D("100.0"))
    assert result.chase_distance == D("0")
    assert result.execution_authority is False


def test_original_zone_is_preserved_and_not_overwritten_by_quote():
    values = inputs()
    original = (values["zone"].lower_price, values["zone"].upper_price,
                values["zone"].zone_hash)
    result = qualify_entry(**values)
    assert result.status is EntryGateStatus.READY
    assert original == (values["zone"].lower_price, values["zone"].upper_price,
                        values["zone"].zone_hash)
    assert result.executable_price != values["zone"].lower_price


def test_thesis_must_be_waiting_for_trigger():
    values = inputs()
    values["thesis"] = waiting_thesis(final_status=ThesisStatus.QUALIFIED)
    result = qualify_entry(**values)
    assert result.reason is EntryGateReason.THESIS_NOT_WAITING_TRIGGER


def test_regime_is_directional_and_fail_closed():
    values = inputs()
    values["regime"] = regime(label=RegimeLabel.RANGE)
    assert qualify_entry(**values).reason is EntryGateReason.REGIME_DIRECTION_BLOCKED
    values["regime"] = regime(available=BASE + timedelta(minutes=11))
    assert qualify_entry(**values).reason is EntryGateReason.REGIME_EVIDENCE_FUTURE


def test_zone_expiry_and_future_zone_are_not_tradable():
    values = inputs()
    values["zone"] = zone(values["thesis"], expires=values["decision_at"])
    assert qualify_entry(**values).reason is EntryGateReason.ZONE_EXPIRED
    values = inputs()
    values["zone"] = zone(values["thesis"], available=BASE + timedelta(minutes=11),
                          expires=BASE + timedelta(minutes=30))
    assert qualify_entry(**values).reason is EntryGateReason.ZONE_EVIDENCE_FUTURE


def test_trigger_must_be_final_allowed_and_after_zone():
    values = inputs()
    values["trigger"] = trigger(values["thesis"], final=False)
    assert qualify_entry(**values).reason is EntryGateReason.TRIGGER_NOT_FINAL
    values["trigger"] = trigger(
        values["thesis"], trigger_type=TriggerType.CLOSED_RECLAIM,
    )
    assert qualify_entry(**values).reason is EntryGateReason.TRIGGER_POLICY_BLOCKED
    values["trigger"] = trigger(values["thesis"], available=BASE + timedelta(minutes=6))
    assert qualify_entry(**values).reason is EntryGateReason.TRIGGER_PRECEDES_ZONE


def test_quote_must_follow_trigger_and_be_fresh():
    values = inputs()
    values["quote"] = quote(available=BASE + timedelta(minutes=9))
    assert qualify_entry(**values).reason is EntryGateReason.QUOTE_PRECEDES_TRIGGER
    values = inputs()
    values["decision_at"] = BASE + timedelta(minutes=10, seconds=10)
    assert qualify_entry(**values).reason is EntryGateReason.QUOTE_STALE


def test_spread_and_chase_limits_are_explicit():
    values = inputs()
    values["quote"] = quote(bid="99", ask="100.5")
    assert qualify_entry(**values).reason is EntryGateReason.SPREAD_TOO_WIDE
    values = inputs()
    values["quote"] = quote(bid="101.9", ask="102.0")
    result = qualify_entry(**values)
    assert result.reason is EntryGateReason.CHASE_DISTANCE_EXCEEDED
    assert result.chase_distance == D("1")
    assert result.maximum_chase_distance == D("0.5")


def test_favorable_but_outside_zone_is_not_silently_reinterpreted():
    values = inputs()
    values["quote"] = quote(bid="98.8", ask="98.9")
    assert qualify_entry(**values).reason is EntryGateReason.EXECUTABLE_PRICE_OUTSIDE_ZONE_POLICY


def test_identity_mismatch_and_future_atr_fail_closed():
    values = inputs()
    values["quote"] = replace(values["quote"], symbol="OTHERUSDT")
    assert qualify_entry(**values).reason is EntryGateReason.IDENTITY_MISMATCH
    values = inputs()
    values["volatility"] = volatility(available=BASE + timedelta(minutes=11))
    assert qualify_entry(**values).reason is EntryGateReason.VOLATILITY_EVIDENCE_FUTURE


def test_contract_epoch_and_thesis_time_are_part_of_causal_identity():
    values = inputs()
    values["quote"] = replace(values["quote"], contract_epoch_id="ABC-PERP-E2")
    assert qualify_entry(**values).reason is EntryGateReason.IDENTITY_MISMATCH

    values = inputs()
    future = transition_thesis(
        values["thesis"], target=ThesisStatus.PROPOSED,
        event_at=BASE + timedelta(minutes=11),
        decision_at=BASE + timedelta(minutes=11),
        evidence_ids=("future-proposal",), reason_code="FUTURE",
    ).snapshot
    future = transition_thesis(
        future, target=ThesisStatus.WAIT_TRIGGER,
        event_at=BASE + timedelta(minutes=11, seconds=1),
        decision_at=BASE + timedelta(minutes=11, seconds=1),
        evidence_ids=("future-retry",), reason_code="FUTURE_RETRY",
    ).snapshot
    values["thesis"] = future
    assert qualify_entry(**values).reason is EntryGateReason.THESIS_EVIDENCE_FUTURE


def test_entry_numeric_output_is_decimal_context_independent():
    observed = []
    for precision in (6, 9, 28, 50):
        with localcontext() as context:
            context.prec = precision
            result = qualify_entry(**inputs())
        observed.append((result.status, result.executable_price, result.spread_fraction,
                         result.qualification_id))
    assert len(set(observed)) == 1


def test_extreme_numeric_input_is_typed_unresolved():
    values = inputs()
    values["quote"] = quote(bid="1E+1000", ask="1E+1000")
    result = qualify_entry(**values)
    assert result.status is EntryGateStatus.UNRESOLVED
    assert result.reason is EntryGateReason.NUMERIC_UNCOMPUTABLE


def test_r6_module_has_no_runtime_or_exchange_surface():
    path = Path(__file__).parents[1] / "strategy_v2" / "r6_entry.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = ("exchange", "binance", "main", "telegram", "risk_manager",
                 "requests", "httpx", "aiohttp", "socket", "sqlite3")
    forbidden_calls = ("create_order", "cancel_order", "set_leverage", "connect",
                       "write_text", "write_bytes", "now", "utcnow")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not item.name.lower().startswith(forbidden) for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert not (node.module or "").lower().startswith(forbidden)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls
