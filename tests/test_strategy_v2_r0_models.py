"""R0 price, time, identity, regime, and reward contracts."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from strategy_v2.causality import CausalTimes, validate_available
from strategy_v2.enums import (DirectionState, InvalidationRelation,
                               InvalidationScope, NoTradeReason, Side,
                               VolatilityState)
from strategy_v2.models import (AttemptId, EntryPriceLineage, EntryZone,
                                ExecutableQuote, ExitPlanContract, ExitTarget,
                                Fill, FillId, FillLedger, IdentityRefs,
                                InvalidationLevel, OrderId, OrderPlanPrice,
                                ProposalId, QuoteId, RegimeSnapshot,
                                RewardReference, SetupId, ThesisId,
                                TriggerEvidence, TriggerId, ZoneId)


T0 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)
T3 = T0 + timedelta(minutes=3)
T4 = T0 + timedelta(minutes=4)
D = Decimal


def times():
    return CausalTimes(T0, T1, T2)


def zone():
    return EntryZone(ZoneId("zone-1"), "ABCUSDT", Side.LONG, D("10"), D("11"),
                     "15m", ("level-1",), times(), T4, "r0", ("evt-level-1",))


def trigger():
    return TriggerEvidence(TriggerId("trigger-1"), ZoneId("zone-1"), "rule-1",
                           "candle-1", D("10.5"), T1, times())


def quote():
    return ExecutableQuote(QuoteId("quote-1"), "ABCUSDT", D("10.6"), D("10.7"),
                           None, T0, T1, T2, "exchange")


def plan():
    return OrderPlanPrice(D("10.7"), "MARKET", "ASK", QuoteId("quote-1"), T3)


def reward():
    return RewardReference("reward-1", D("14"), Side.LONG, "level-2", times())


def test_zone_is_immutable_and_midpoint_is_derived():
    item = zone()
    assert item.midpoint == D("10.5")
    with pytest.raises(FrozenInstanceError):
        item.lower = D("12")
    assert item.source_event_ids == ("evt-level-1",)


@pytest.mark.parametrize("lower,upper", [(D("0"), D("1")), (D("2"), D("1"))])
def test_invalid_zone_geometry_rejected(lower, upper):
    with pytest.raises(ValueError):
        replace(zone(), lower=lower, upper=upper)


def test_price_roles_remain_distinct_and_partial_fills_preserved():
    fills = FillLedger((
        Fill(FillId("fill-1"), OrderId("order-1"), D("10.8"), D("2"), D("0.01"), "USDT", T4),
        Fill(FillId("fill-2"), OrderId("order-1"), D("11.1"), D("1"), D("0.01"), "USDT", T4),
    ))
    lineage = EntryPriceLineage(zone(), trigger(), quote(), plan(), fills, T3)
    assert lineage.original_zone.midpoint == D("10.5")
    assert lineage.trigger.trigger_price == D("10.5")
    assert lineage.executable_quote.best_ask == D("10.7")
    assert lineage.order_plan_price.planned_price == D("10.7")
    assert fills.total_quantity == D("3")
    assert fills.average_price == D("10.9")
    assert tuple(fill.fill_id.value for fill in fills.fills) == ("fill-1", "fill-2")
    with pytest.raises(FrozenInstanceError):
        fills.fills = ()


def test_order_plan_never_becomes_a_fill():
    assert type(plan()) is OrderPlanPrice
    assert type(Fill(FillId("fill-1"), OrderId("order-1"), D("11"), D("1"), D("0"), "USDT", T4)) is Fill
    assert FillLedger().average_price is None


def test_duplicate_fill_rejected():
    fill = Fill(FillId("fill-1"), OrderId("order-1"), D("10"), D("1"), D("0"), "USDT", T4)
    with pytest.raises(ValueError, match="duplicate fill_id"):
        FillLedger((fill, fill))


@pytest.mark.parametrize("change", [
    {"trigger": TriggerEvidence(TriggerId("other"), ZoneId("wrong"), "r", "c", D("10"), T1, times())},
    {"executable_quote": ExecutableQuote(QuoteId("other"), "OTHER", D("10"), D("11"), None, T0, T1, T2, "x")},
    {"order_plan_price": OrderPlanPrice(D("10"), "MARKET", "ASK", QuoteId("other"), T3)},
])
def test_lineage_rejects_crossed_ids_or_symbols(change):
    args = {"original_zone": zone(), "trigger": trigger(), "executable_quote": quote(),
            "order_plan_price": plan(), "fills": FillLedger(), "decision_at": T3}
    args.update(change)
    with pytest.raises(ValueError):
        EntryPriceLineage(**args)


def test_quote_not_available_at_decision_rejected():
    with pytest.raises(ValueError, match="unavailable"):
        EntryPriceLineage(zone(), trigger(), quote(), plan(), FillLedger(), T1)


def test_lineage_rejects_trigger_before_original_zone_available():
    late_zone = replace(zone(), times=CausalTimes(T0, T1, T3))
    with pytest.raises(ValueError, match="causal ordering"):
        EntryPriceLineage(late_zone, trigger(), quote(), plan(), FillLedger(), T4)


def test_lineage_rejects_quote_before_trigger_available():
    late_trigger = replace(trigger(), times=CausalTimes(T0, T1, T3))
    with pytest.raises(ValueError, match="causal ordering"):
        EntryPriceLineage(zone(), late_trigger, quote(), plan(), FillLedger(), T4)


def test_lineage_rejects_order_plan_before_quote_available():
    early_plan = replace(plan(), planned_at=T1)
    with pytest.raises(ValueError, match="causal ordering"):
        EntryPriceLineage(zone(), trigger(), quote(), early_plan, FillLedger(), T4)


def test_lineage_rejects_fill_before_order_plan():
    early_fill = Fill(FillId("fill-early"), OrderId("order-1"), D("10.8"),
                      D("1"), D("0"), "USDT", T2)
    with pytest.raises(ValueError, match="fill cannot precede"):
        EntryPriceLineage(zone(), trigger(), quote(), plan(),
                          FillLedger((early_fill,)), T3)


def test_lineage_accepts_fill_after_decision_as_outcome():
    later_fill = Fill(FillId("fill-later"), OrderId("order-1"), D("10.8"),
                      D("1"), D("0"), "USDT", T4)
    lineage = EntryPriceLineage(zone(), trigger(), quote(), plan(),
                                FillLedger((later_fill,)), T3)
    assert lineage.fills.fills[0].filled_at > lineage.decision_at


def test_valid_causal_ordering():
    assert validate_available(times(), decision_at=T3).valid
    assert times().source_at <= times().confirmed_at <= times().available_at <= T3


def test_future_evidence_rejected_with_typed_reason():
    result = validate_available(times(), decision_at=T1)
    assert not result.valid
    assert result.reason_code is NoTradeReason.FUTURE_EVIDENCE


@pytest.mark.parametrize("kwargs", [
    {"source_at": None}, {"confirmed_at": None}, {"available_at": None},
    {"confirmed_at": datetime(2026, 10, 1, 10, 1)},
    {"available_at": T0},
])
def test_missing_naive_or_out_of_order_provenance_rejected(kwargs):
    values = {"source_at": T0, "confirmed_at": T1, "available_at": T2}
    values.update(kwargs)
    with pytest.raises(ValueError):
        CausalTimes(**values)


def test_no_timestamp_fallback_from_decision_clock():
    with pytest.raises(TypeError):
        CausalTimes(source_at=T0, confirmed_at=T1)


def test_received_time_cannot_precede_source_time():
    with pytest.raises(ValueError, match="received_at"):
        CausalTimes(T1, T2, T3, received_at=T0)


def test_identifier_tuples_cannot_hide_mutable_members():
    with pytest.raises(ValueError, match="source_level_ids"):
        replace(zone(), source_level_ids=([],))
    with pytest.raises(ValueError, match="source_event_ids"):
        replace(zone(), source_event_ids=(["mutable"],))
    with pytest.raises(ValueError, match="exit_event_types"):
        ExitPlanContract("runner", reward(), None, False, (["mutable"],))


@pytest.mark.parametrize("direction", list(DirectionState))
@pytest.mark.parametrize("volatility", list(VolatilityState))
def test_all_16_regime_axis_combinations_are_representable(direction, volatility):
    snapshot = RegimeSnapshot(direction, volatility, T3, ("bar-1",))
    assert snapshot.direction_state is direction
    assert snapshot.volatility_state is volatility


def test_typed_identities_are_not_interchangeable():
    assert ThesisId("same") != SetupId("same")
    with pytest.raises(TypeError):
        IdentityRefs(thesis_id=SetupId("setup-1"))
    with pytest.raises(ValueError, match="reused"):
        IdentityRefs(thesis_id=ThesisId("same"), proposal_id=ProposalId("same"))
    refs = IdentityRefs(thesis_id=ThesisId("thesis-1"), setup_id=SetupId("setup-1"),
                        attempt_id=AttemptId("attempt-1"), proposal_id=ProposalId("proposal-1"))
    assert refs.thesis_id != refs.proposal_id


def test_invalidation_requires_causal_provenance_and_thesis_or_setup():
    item = InvalidationLevel("level-1", InvalidationScope.THESIS, D("9"),
                             InvalidationRelation.BREAK_BELOW, Side.LONG,
                             "4h swing", "4h", times(), thesis_id=ThesisId("thesis-1"))
    assert item.scope is InvalidationScope.THESIS
    with pytest.raises(TypeError):
        replace(item, times=None)
    with pytest.raises(ValueError):
        replace(item, thesis_id=None)


def test_invalidation_scope_has_one_unambiguous_four_tier_hierarchy():
    assert [scope.value for scope in InvalidationScope] == [
        "MICRO", "SETUP", "THESIS", "MACRO",
    ]
    assert "ATTEMPT" not in InvalidationScope.__members__
    assert AttemptId("attempt-1").value == "attempt-1"


def test_reward_reference_is_independent_of_optional_exit_target():
    runner = ExitPlanContract("runner-v0", reward(), None, False,
                              ("STRUCTURE_EXIT", "TIME_EXIT"))
    assert runner.reward_reference is not None and runner.exit_target is None
    target = ExitTarget("target-1", D("14"), Side.LONG, "level-2", times())
    planned = ExitPlanContract("target-v0", reward(), target, True, ("FINAL_TP",))
    assert planned.exit_target is target


def test_reward_reference_and_policy_are_explicit_requirements():
    with pytest.raises(ValueError, match="reward_reference"):
        ExitPlanContract("runner", None, None, False, ("STRUCTURE_EXIT",))
    with pytest.raises(ValueError, match="target"):
        ExitPlanContract("target", reward(), None, True, ("FINAL_TP",))
    with pytest.raises(ValueError, match="exit event"):
        ExitPlanContract("runner", reward(), None, False, ())
