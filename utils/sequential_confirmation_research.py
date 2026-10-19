"""Frozen research-only sweep carry experiment; no runtime imports or orders."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

POLICY = "SWEEP_THEN_VOLUME_NEXT_5M_V1"

@dataclass(frozen=True)
class Context:
    # Include run, order, side, profile and frozen geometry in this identity.
    setup_key: str
    observed_at: datetime
    zone_touched_at: datetime | None
    deadline: datetime
    invalidation_state: str = "VALID"
    continuity_epoch: int = 0

@dataclass(frozen=True)
class Observation:
    confirmation: dict
    context: Context

def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("An aware timestamp is required")
    return value.astimezone(timezone.utc)

def _bar(confirmation, observed):
    bar = confirmation["closed_bar"]
    opened, closed = _utc(bar["open_time"]), _utc(bar["close_time"])
    observed = _utc(observed)
    step = 300_000
    expected = (int(observed.timestamp() * 1000) // step - 1) * step
    valid = (
        bar.get("closed") is True
        and bar.get("exact_closed_bar") is True
        and int(confirmation["bar_id"]) == expected == int(opened.timestamp() * 1000)
        and closed == opened + timedelta(minutes=5)
        and closed <= observed < closed + timedelta(minutes=5)
        and confirmation["evidence"].get("data_fresh") is True
    )
    if not valid:
        raise ValueError("Not the exact available closed bar")
    return opened, closed

def evaluate(current: dict, context: Context, previous: Observation | None = None):
    """Carry a qualified sweep only to the immediately following closed bar.

    A qualifying earlier observation must already pass every non-participation
    gate after a recorded zone touch. Current volume and every other current
    gate remain mandatory. Missing, reordered or interrupted evidence cannot
    authorize the alternative. This evaluates recorded gates, not fillability.
    """
    result = deepcopy(current)
    observation = Observation(deepcopy(current), context)
    diagnostic = {"policy": POLICY, "changed": False, "reason": "BASELINE_UNCHANGED"}
    result["sequential"] = diagnostic
    if current.get("passed") is True:
        return result, observation
    if current.get("passed") is not False or current.get("failed_gates") != ["participation"]:
        diagnostic["reason"] = "OTHER_CURRENT_GATES_BLOCK"
        return result, observation
    if previous is None:
        diagnostic["reason"] = "NO_PREVIOUS_OBSERVATION"
        return result, observation
    prior, pc = previous.confirmation, previous.context
    try:
        now, prior_at = _utc(context.observed_at), _utc(pc.observed_at)
        opened, closed = _bar(current, now)
        p_opened, p_closed = _bar(prior, prior_at)
        ev, pe = current["evidence"], prior["evidence"]
        checks = {
            "SAME_SETUP": bool(context.setup_key) and context.setup_key == pc.setup_key,
            "UNINTERRUPTED": context.continuity_epoch == pc.continuity_epoch,
            "VALID_STRUCTURE": context.invalidation_state == pc.invalidation_state == "VALID",
            "CHRONOLOGICAL": prior_at < now and prior_at < closed,
            "CONSECUTIVE_BARS": opened == p_opened + timedelta(minutes=5),
            "SAME_SOURCE": bool(current["closed_bar"].get("source")) and current["closed_bar"].get("source") == prior["closed_bar"].get("source"),
            "WITHIN_DEADLINE": now < _utc(context.deadline) and context.deadline == pc.deadline,
            "SAME_ZONE_TOUCH": context.zone_touched_at is not None and context.zone_touched_at == pc.zone_touched_at,
            "SWEEP_AFTER_TOUCH": context.zone_touched_at is not None and _utc(context.zone_touched_at) <= p_closed,
            "PREVIOUS_OTHER_GATES_PASS": prior.get("passed") is False and prior.get("failed_gates") == ["participation"],
            "STRICT_PROFILE": all(x.get("require_entry_sweep") is True and x.get("require_volume") is True and x.get("participation_required") == 2 for x in (ev, pe)),
            "NO_COUNTERTREND": all(x.get("countertrend") is False and x.get("regime_opposes") is False for x in (ev, pe)),
            "PREVIOUS_SWEEP_ONLY": pe.get("swept_liquidity") is True and pe.get("volume_confirmed") is False,
            "CURRENT_VOLUME": ev.get("volume_confirmed") is True,
            "CURRENT_OTHER_EVIDENCE": all(ev.get(k) is True and pe.get(k) is True for k in ("structure_valid", "local_trend_aligned", "regime_policy", "entry_location_valid", "directional_close", "reclaim", "not_invalidated")),
            "TARGET_ROOM": all(math.isfinite(float(x["planned_rr"])) and float(x["planned_rr"]) >= float(x["min_rr"]) > 0 for x in (ev, pe)),
        }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        diagnostic["reason"] = "MALFORMED_OR_UNAVAILABLE_EVIDENCE"
        return result, observation
    failures = [k for k, passed in checks.items() if not passed]
    if failures:
        diagnostic["reason"] = ",".join(failures)
        return result, observation
    result.update(passed=True, failed_gates=[], reason="CONFIRMED_SEQUENTIAL_SWEEP")
    result["evidence"]["sequential_sweep_used"] = True
    diagnostic.update(changed=True, reason="PREVIOUS_SWEEP_CURRENT_VOLUME", sweep_bar_id=prior["bar_id"])
    return result, observation
