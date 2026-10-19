from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest
from utils.sequential_confirmation_research import Context, Observation, evaluate

NOW = datetime(2026, 9, 8, 16, 5, 18, tzinfo=timezone.utc)

def candle(now, sweep=False, volume=True):
    closed = now.replace(second=0, microsecond=0)
    opened = closed - timedelta(minutes=5)
    return {"passed": False, "failed_gates": ["participation"], "bar_id": str(int(opened.timestamp()*1000)),
        "closed_bar": {"closed": True, "exact_closed_bar": True, "source": "DEMO",
            "open_time": opened.isoformat(), "close_time": closed.isoformat()},
        "evidence": {"data_fresh": True, "require_entry_sweep": True, "require_volume": True,
            "participation_required": 2, "countertrend": False, "regime_opposes": False,
            "swept_liquidity": sweep, "volume_confirmed": volume, "planned_rr": 2, "min_rr": 1.5,
            **{k: True for k in ("structure_valid", "local_trend_aligned", "regime_policy", "entry_location_valid", "directional_close", "reclaim", "not_invalidated")}}}

def setup():
    ctx = Context("run:order:sell:strict:64:66:62", NOW, NOW-timedelta(minutes=11), NOW+timedelta(minutes=1))
    pc = replace(ctx, observed_at=NOW-timedelta(minutes=5))
    return candle(NOW), ctx, Observation(candle(pc.observed_at, True, False), pc)

def test_next_bar_volume_after_qualified_sweep_passes_without_mutating_inputs():
    current, ctx, prev = setup()
    untouched = deepcopy(current), deepcopy(prev.confirmation)
    result, saved = evaluate(current, ctx, prev)
    assert result["passed"] and result["sequential"]["changed"]
    assert not result["evidence"]["swept_liquidity"]
    assert result["evidence"]["sequential_sweep_used"]
    assert (current, prev.confirmation) == untouched
    result["evidence"]["data_fresh"] = False
    assert saved.confirmation["evidence"]["data_fresh"]

@pytest.mark.parametrize("change", [
    {"setup_key": "another_order"}, {"invalidation_state": "SWEEP_PENDING"},
    {"invalidation_state": "HARD_INVALIDATED"}, {"invalidation_state": "RECLAIM_CONFIRMED"},
    {"continuity_epoch": 1}, {"zone_touched_at": None}, {"deadline": NOW},
    {"zone_touched_at": NOW-timedelta(minutes=1)},
])
def test_lifecycle_barriers_block_carry(change):
    current, ctx, prev = setup()
    assert not evaluate(current, replace(ctx, **change), prev)[0]["passed"]

@pytest.mark.parametrize("which,change", [
    ("current", {"volume_confirmed": False}), ("previous", {"swept_liquidity": False}),
    ("current", {"countertrend": True}), ("previous", {"regime_opposes": True}),
    ("current", {"entry_location_valid": False}), ("previous", {"not_invalidated": False}),
    ("current", {"data_fresh": False}), ("current", {"planned_rr": 1.4}),
    ("current", {"planned_rr": float("nan")}), ("current", {"require_entry_sweep": False}),
    ("current", {"participation_required": 3}),
])
def test_other_gates_and_profile_requirements_are_preserved(which, change):
    current, ctx, prev = setup()
    (current if which == "current" else prev.confirmation)["evidence"].update(change)
    assert not evaluate(current, ctx, prev)[0]["passed"]

@pytest.mark.parametrize("delta", [0, -10, 5])
def test_duplicate_missing_and_future_bars_cannot_supply_sweep(delta):
    current, ctx, prev = setup()
    pc = replace(prev.context, observed_at=NOW+timedelta(minutes=delta))
    prev = Observation(candle(pc.observed_at, True, False), pc)
    assert not evaluate(current, ctx, prev)[0]["passed"]

def test_same_bar_reversal_does_not_accumulate_evidence():
    current, ctx, prev = setup()
    prev = Observation(candle(NOW, True, False), replace(prev.context, observed_at=NOW-timedelta(seconds=1)))
    assert not evaluate(current, ctx, prev)[0]["passed"]

def test_closed_future_bar_and_malformed_bar_fail_closed():
    current, ctx, prev = setup()
    for changed in (candle(NOW+timedelta(minutes=5)), {**current, "bar_id": "broken"}):
        assert not evaluate(changed, ctx, prev)[0]["passed"]

def test_current_and_prior_non_participation_failures_are_not_removed():
    current, ctx, prev = setup()
    current["failed_gates"].append("directional_reclaim")
    assert not evaluate(current, ctx, prev)[0]["passed"]
    current["failed_gates"] = ["participation"]
    prev.confirmation["failed_gates"].append("entry_location")
    assert not evaluate(current, ctx, prev)[0]["passed"]

def test_strict_baseline_pass_remains_unchanged_and_needs_no_history():
    current, ctx, _ = setup()
    current.update(passed=True, failed_gates=[], reason="CONFIRMED")
    result, _ = evaluate(current, ctx)
    assert result["passed"] and not result["sequential"]["changed"]

def test_different_venue_and_naive_clock_are_rejected():
    current, ctx, prev = setup()
    prev.confirmation["closed_bar"]["source"] = "LIVE"
    assert not evaluate(current, ctx, prev)[0]["passed"]
    current, ctx, prev = setup()
    assert not evaluate(current, replace(ctx, observed_at=NOW.replace(tzinfo=None)), prev)[0]["passed"]

def test_current_direction_still_required_even_with_previous_sweep():
    current, ctx, prev = setup()
    current["evidence"]["directional_close"] = False
    assert not evaluate(current, ctx, prev)[0]["passed"]

def test_missing_nested_evidence_fails_closed():
    current, ctx, prev = setup()
    current["evidence"] = None
    result, _ = evaluate(current, ctx, prev)
    assert result["passed"] is False

def test_unknown_baseline_state_cannot_be_promoted():
    current, ctx, prev = setup()
    del current["passed"]
    result, _ = evaluate(current, ctx, prev)
    assert result.get("passed") is not True

def test_missing_previous_nested_evidence_fails_closed():
    current, ctx, prev = setup()
    prev.confirmation["evidence"] = None
    assert not evaluate(current, ctx, prev)[0]["passed"]
