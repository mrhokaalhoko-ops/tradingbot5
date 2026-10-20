"""Causal Strategy V2 evidence collection with zero execution authority.

The module accepts immutable decision-time inputs, produces research records,
and persists them through an injected state manager.  It deliberately has no
exchange/client dependency and exposes no order, leverage, sizing, SL, or TP
mutation method.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from strategy.strategy_v2_research import (
    evaluate_causal_opportunity,
    evaluate_thesis_reentry,
    select_thesis_invalidation,
    structural_management_candidate,
)


SCHEMA_VERSION = 1
COLLECTOR_VERSION = "STRATEGY_V2_EVIDENCE_V1_FROZEN"
RESET_EVENT_TYPES = {
    "NEW_BOS", "NEW_CHOCH", "NEW_LIQUIDITY_SWEEP", "NEW_ORDER_BLOCK",
    "NEW_ENTRY_ZONE", "INVALIDATION_AND_RECLAIM",
    "MATERIAL_HTF_CONTEXT_CHANGE",
}


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        try:
            if raw.isdigit():
                numeric = int(raw)
                seconds = numeric / 1000.0 if numeric > 10_000_000_000 else numeric
                parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, OSError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    parsed = _utc(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else ""


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:20]}"


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


EVIDENCE_TIMESTAMP_FIELDS = (
    "confirmed_at", "known_at", "last_closed_at", "closed_at", "created_at", "formed_at",
    "observed_at", "timestamp", "time", "bar_time",
)


def _evidence_timestamp(value: Mapping[str, Any] | None) -> tuple[datetime | None, str | None]:
    """Return an explicitly supplied source timestamp; never infer one."""
    item = dict(value or {})
    for field in EVIDENCE_TIMESTAMP_FIELDS:
        if item.get(field) not in (None, ""):
            return _utc(item.get(field)), field
    return None, None


def _component_integrity(
    component: str,
    evidence: Iterable[tuple[str, Mapping[str, Any] | None]],
    *,
    decision_timestamp: Any,
) -> dict[str, Any]:
    """Validate every supplied evidence item against the decision clock."""
    decision_at = _utc(decision_timestamp)
    if not decision_at:
        raise ValueError("decision_timestamp must be valid UTC")
    inspected, violations, source_times = [], [], []
    for label, raw in evidence:
        item = dict(raw or {})
        source_at, source_field = _evidence_timestamp(item)
        if source_at:
            source_times.append(source_at)
        if source_field is None or source_at is None:
            reason, causal = "MISSING_SOURCE_TIMESTAMP", False
        elif source_at > decision_at:
            reason, causal = "LOOKAHEAD_SOURCE_TIMESTAMP", False
        else:
            reason, causal = "KNOWN_AT_DECISION", True
        inspected_item = {
            "label": str(label),
            "source_timestamp": _iso(source_at),
            "source_timestamp_field": source_field,
            "causal_at_decision": causal,
            "reason": reason,
        }
        inspected.append(inspected_item)
        if not causal:
            violations.append({
                "component": component,
                "label": str(label),
                "reason": reason,
                "timestamp": _iso(source_at),
            })
    lookahead = any(item["reason"] == "LOOKAHEAD_SOURCE_TIMESTAMP" for item in inspected)
    missing = any(item["reason"] == "MISSING_SOURCE_TIMESTAMP" for item in inspected)
    latest = max(source_times) if source_times else None
    return {
        "component": component,
        "evidence": inspected,
        "latest_source_timestamp": _iso(latest),
        "lookahead_violation": lookahead,
        "missing_source_timestamps": missing,
        "valid_for_research": not lookahead and not missing,
        "integrity_violations": violations,
    }


def _ta_context_integrity(
    context: Mapping[str, Any] | None, *, decision_timestamp: Any,
) -> dict[str, Any]:
    context_copy = dict(context or {})
    evidence: list[tuple[str, Mapping[str, Any]]] = []
    anchor = context_copy.get("setup_anchor")
    if isinstance(anchor, Mapping) and anchor:
        evidence.append(("setup_anchor", dict(anchor)))
    for timeframe in ("1w", "1d", "4h", "1h", "15m", "5m"):
        value = context_copy.get(timeframe)
        if isinstance(value, Mapping) and value:
            evidence.append((f"ta_context.{timeframe}", dict(value)))
    for field in (
        "bos_created_at", "choch_created_at", "liquidity_sweep_at",
        "entry_zone_created_at", "htf_context_changed_at",
    ):
        if context_copy.get(field) not in (None, ""):
            evidence.append((field, {"timestamp": context_copy.get(field)}))
    return _component_integrity(
        "ta_context_integrity", evidence,
        decision_timestamp=decision_timestamp,
    )


def _thesis_integrity(
    stop_plan: Mapping[str, Any] | None, *, decision_timestamp: Any,
) -> dict[str, Any]:
    plan = dict(stop_plan or {})
    explicit = _number(plan.get("selected_thesis_invalidation"))
    if explicit is None:
        return _component_integrity(
            "thesis_integrity", [], decision_timestamp=decision_timestamp,
        )
    timestamp_payload = {
        key: plan.get(key) for key in (
            "confirmed_at", "created_at", "timestamp",
            "structural_level_confirmed_at", "structural_level_created_at",
            "invalidation_confirmed_at", "invalidation_created_at",
        ) if plan.get(key) not in (None, "")
    }
    # Normalize named invalidation fields into the explicit validator schema.
    timestamp_payload["confirmed_at"] = (
        timestamp_payload.get("confirmed_at")
        or timestamp_payload.get("structural_level_confirmed_at")
        or timestamp_payload.get("invalidation_confirmed_at")
    )
    timestamp_payload["created_at"] = (
        timestamp_payload.get("created_at")
        or timestamp_payload.get("structural_level_created_at")
        or timestamp_payload.get("invalidation_created_at")
    )
    return _component_integrity(
        "thesis_integrity",
        [("explicit_thesis_invalidation", timestamp_payload)],
        decision_timestamp=decision_timestamp,
    )


def _reset_integrity(
    reset_evidence: Iterable[Mapping[str, Any]] | None, *,
    decision_timestamp: Any, prior_closed_at: Any = None,
) -> dict[str, Any]:
    records = [dict(item) for item in (reset_evidence or []) if isinstance(item, Mapping)]
    component = _component_integrity(
        "reset_integrity",
        [(f"reset_event[{index}]", item) for index, item in enumerate(records)],
        decision_timestamp=decision_timestamp,
    )
    closed_at = _utc(prior_closed_at)
    for index, inspected in enumerate(component["evidence"]):
        record = records[index]
        event = str(record.get("event") or record.get("type") or "").upper()
        source_at = _utc(inspected.get("source_timestamp"))
        inspected["event"] = event or None
        inspected["eligible_reset"] = bool(
            inspected["causal_at_decision"]
            and event in RESET_EVENT_TYPES
            and (closed_at is None or (source_at and source_at > closed_at))
        )
        if inspected["causal_at_decision"] and not inspected["eligible_reset"]:
            inspected["reset_rejection_reason"] = (
                "NOT_AFTER_PRIOR_CLOSE"
                if closed_at is not None and source_at and source_at <= closed_at
                else "UNSUPPORTED_RESET_EVENT"
            )
    return component


def _aggregate_integrity(**components: Mapping[str, Any]) -> dict[str, Any]:
    values = [dict(value) for value in components.values()]
    violations = []
    latest = []
    for value in values:
        violations.extend(copy.deepcopy(value.get("integrity_violations") or []))
        stamp = _utc(value.get("latest_source_timestamp"))
        if stamp:
            latest.append(stamp)
    valid = all(bool(value.get("valid_for_research")) for value in values)
    return {
        **{name: copy.deepcopy(dict(value)) for name, value in components.items()},
        "max_source_timestamp_lte_decision": not any(
            bool(value.get("lookahead_violation")) for value in values
        ),
        "lookahead_violation": any(
            bool(value.get("lookahead_violation")) for value in values
        ),
        "missing_source_timestamps": any(
            bool(value.get("missing_source_timestamps")) for value in values
        ),
        "latest_source_timestamp": _iso(max(latest) if latest else None),
        "valid_for_research": valid,
        "integrity_violations": violations,
    }


def _iter_market_levels(market_levels: Mapping[str, Any] | None):
    for outer_key, value in dict(market_levels or {}).items():
        outer = str(outer_key).lower()
        if outer.startswith("_"):
            continue
        if outer in {"1w", "1d", "4h", "1h", "15m", "5m"} and isinstance(value, Mapping):
            for family, members in value.items():
                for item in _records(members):
                    yield outer, str(family), item
        else:
            for item in _records(value):
                yield str(item.get("timeframe") or "15m").lower(), outer, item


def _price_fields(item: Mapping[str, Any]) -> tuple[float | None, float | None, float | None]:
    low = _number(item.get("zone_low") or item.get("low") or item.get("bottom"))
    high = _number(item.get("zone_high") or item.get("high") or item.get("top"))
    price = _number(item.get("price") or item.get("level"))
    if price is None and low is not None and high is not None:
        price = (low + high) / 2.0
    elif price is None:
        price = low if low is not None else high
    return price, low, high


def build_causal_level_snapshot(
    *,
    market_levels: Mapping[str, Any] | None,
    decision_timestamp: Any,
    entry: float,
    side: str,
) -> dict[str, Any]:
    """Snapshot every supplied level and prove its decision-time causality."""
    decision_at = _utc(decision_timestamp)
    if not decision_at:
        raise ValueError("decision_timestamp must be valid UTC")
    levels, source_times, violations = [], [], []
    side_l = str(side or "").lower()
    for timeframe, family, item in _iter_market_levels(market_levels):
        price, zone_low, zone_high = _price_fields(item)
        if price is None or price <= 0:
            continue
        created = _utc(item.get("created_at") or item.get("timestamp") or item.get("time"))
        confirmed = _utc(item.get("confirmed_at")) or created
        source_at = max([stamp for stamp in (created, confirmed) if stamp], default=None)
        causal = bool(source_at and source_at <= decision_at)
        if source_at:
            source_times.append(source_at)
        reason = (
            "KNOWN_AT_DECISION" if causal else
            "MISSING_SOURCE_TIMESTAMP" if source_at is None else
            "LOOKAHEAD_SOURCE_TIMESTAMP"
        )
        thesis_link = str(
            item.get("thesis_link") or item.get("relation_to_thesis") or ""
        )
        if reason != "KNOWN_AT_DECISION":
            violations.append({"family": family, "reason": reason, "timestamp": _iso(source_at)})
        target_side = price > entry if side_l in {"buy", "long"} else price < entry
        role = []
        if target_side:
            role.append("OPPORTUNITY")
        else:
            role.append("INVALIDATION_CANDIDATE")
        identity = {
            "family": family, "timeframe": timeframe, "price": price,
            "zone_low": zone_low, "zone_high": zone_high,
            "created_at": _iso(created), "confirmed_at": _iso(confirmed),
        }
        levels.append({
            "level_id": str(item.get("level_id") or _stable_id("lvl", identity)),
            "type": family,
            "price": price,
            "zone_low": zone_low,
            "zone_high": zone_high,
            "timeframe": timeframe,
            "created_at": _iso(created),
            "confirmed_at": _iso(confirmed),
            "age": item.get("age"),
            "source": str(item.get("source") or family),
            "strength": _number(item.get("strength") or item.get("quality")) or 0.0,
            "touch_count": int(_number(item.get("touch_count")) or 0),
            "freshness": item.get("freshness"),
            "causal_at_decision": causal,
            "causal_reason": reason,
            "thesis_link": thesis_link or None,
            "role_candidate": role,
        })
    latest = max(source_times) if source_times else None
    lookahead = bool(latest and latest > decision_at)
    missing = any(item["causal_reason"] == "MISSING_SOURCE_TIMESTAMP" for item in levels)
    return {
        "levels": levels,
        "decision_timestamp": _iso(decision_at),
        "latest_allowed_market_timestamp": _iso(latest),
        "max_source_timestamp_lte_decision": not lookahead,
        "lookahead_violation": lookahead,
        "missing_source_timestamps": missing,
        "valid_for_research": not lookahead and not missing,
        "integrity_violations": violations,
    }


def build_thesis_object(
    *, proposal: Mapping[str, Any], ta_context: Mapping[str, Any] | None,
    stop_plan: Mapping[str, Any] | None, level_snapshot: Mapping[str, Any],
    thesis_integrity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context, plan = dict(ta_context or {}), dict(stop_plan or {})
    anchor = dict(context.get("setup_anchor") or {})
    explicit = _number(plan.get("selected_thesis_invalidation"))
    relation = str(plan.get("relation_to_thesis") or "")
    proven = bool(explicit and relation in {
        "THESIS_STRUCTURE", "THESIS_INVALIDATION", "EXPLICIT_THESIS_INVALIDATION"
    } and thesis_integrity and thesis_integrity.get("valid_for_research") is True)
    linked = [
        item["level_id"] for item in level_snapshot.get("levels", [])
        if item.get("thesis_link")
    ]
    by_type = lambda token: [
        item["level_id"] for item in level_snapshot.get("levels", [])
        if token in str(item.get("type") or "").lower() and item.get("thesis_link")
    ]
    thesis_id = str(proposal.get("thesis_id") or proposal.get("setup_id") or "")
    return {
        "thesis_id": thesis_id or None,
        "symbol": proposal.get("symbol"),
        "side": proposal.get("side"),
        "created_at": proposal.get("timestamp"),
        "entry_anchor": anchor or None,
        "entry_anchor_type": anchor.get("type") or None,
        "explicit_thesis_invalidation": explicit if proven else None,
        "invalidation_reason": (
            relation if proven else "THESIS_INVALIDATION_NOT_PROVEN"
        ),
        "invalidation_level_id": plan.get("invalidation_level_id") if proven else None,
        "supporting_structure_ids": linked,
        "supporting_OB_ids": by_type("ob"),
        "supporting_FVG_ids": by_type("fvg"),
        "supporting_liquidity_ids": by_type("liquidity"),
        "HTF_context": {
            key: copy.deepcopy(context.get(key)) for key in ("1w", "1d", "4h", "1h")
            if context.get(key) is not None
        },
        "trigger_context": {
            "15m": copy.deepcopy(context.get("15m")),
            "5m": copy.deepcopy(context.get("5m")),
        },
    }


def _target_matches_level(target: float, entry: float, tick_size: float, levels: Iterable[Mapping[str, Any]]) -> bool:
    tolerance = max(abs(entry) * 0.0005, abs(tick_size) * 2.0, 1e-12)
    for item in levels:
        low, high = _number(item.get("zone_low")), _number(item.get("zone_high"))
        if low is not None and high is not None and low - tolerance <= target <= high + tolerance:
            return True
        price = _number(item.get("price"))
        if price is not None and abs(price - target) <= tolerance:
            return True
    return False


def build_shadow_decision_record(
    *,
    proposal: Mapping[str, Any],
    market_levels: Mapping[str, Any] | None,
    ta_context: Mapping[str, Any] | None,
    reset_evidence: Iterable[Mapping[str, Any]] | None,
    prior_closed_thesis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build decision-only evidence. Outcome data is not an accepted input."""
    proposal_copy = copy.deepcopy(dict(proposal))
    context = copy.deepcopy(dict(ta_context or {}))
    decision_at = proposal_copy.get("timestamp")
    entry = float(proposal_copy["proposed_entry"])
    stop = float(proposal_copy["original_stop"])
    target = float(proposal_copy["original_target"])
    side = str(proposal_copy["side"])
    target_plan = dict(proposal_copy.get("target_plan") or {})
    stop_plan = dict(target_plan.get("stop_plan") or {})
    level_snapshot = build_causal_level_snapshot(
        market_levels=market_levels, decision_timestamp=decision_at,
        entry=entry, side=side,
    )
    market_integrity = {
        "component": "market_level_integrity",
        "evidence": [
            {
                "label": item.get("level_id"),
                "source_timestamp": item.get("confirmed_at") or item.get("created_at"),
                "source_timestamp_field": (
                    "confirmed_at" if item.get("confirmed_at") else "created_at"
                ),
                "causal_at_decision": item.get("causal_at_decision"),
                "reason": item.get("causal_reason"),
            }
            for item in level_snapshot.get("levels", [])
        ],
        "latest_source_timestamp": level_snapshot.get("latest_allowed_market_timestamp"),
        "lookahead_violation": level_snapshot.get("lookahead_violation", False),
        "missing_source_timestamps": level_snapshot.get("missing_source_timestamps", False),
        "valid_for_research": level_snapshot.get("valid_for_research", False),
        "integrity_violations": [
            {"component": "market_level_integrity", **dict(item)}
            for item in level_snapshot.get("integrity_violations", [])
        ],
    }
    ta_integrity = _ta_context_integrity(context, decision_timestamp=decision_at)
    thesis_integrity = _thesis_integrity(stop_plan, decision_timestamp=decision_at)
    prior = dict(prior_closed_thesis or {})
    reset_integrity = _reset_integrity(
        reset_evidence, decision_timestamp=decision_at,
        prior_closed_at=prior.get("closed_at"),
    )
    aggregate_integrity = _aggregate_integrity(
        market_level_integrity=market_integrity,
        ta_context_integrity=ta_integrity,
        thesis_integrity=thesis_integrity,
        reset_integrity=reset_integrity,
    )
    thesis = build_thesis_object(
        proposal=proposal_copy, ta_context=context, stop_plan=stop_plan,
        level_snapshot=level_snapshot, thesis_integrity=thesis_integrity,
    )
    target_shadow = evaluate_causal_opportunity(
        side=side, entry=entry, initial_stop=stop,
        market_levels=dict(market_levels or {}), decision_time=decision_at,
        minimum_rr=float(target_plan.get("min_rr") or 1.5),
    )
    stop_shadow = select_thesis_invalidation(
        side=side, entry=entry, setup_anchor=context.get("setup_anchor"),
        explicit_invalidation=thesis.get("explicit_thesis_invalidation"),
        market_levels=dict(market_levels or {}), decision_time=decision_at,
    )
    if prior:
        reentry_shadow = evaluate_thesis_reentry(
            old_thesis_id=prior.get("thesis_id"),
            new_thesis_id=thesis.get("thesis_id"),
            prior_closed_at=prior.get("closed_at"),
            reset_evidence=list(reset_evidence or []),
            decision_time=decision_at,
        )
    else:
        reentry_shadow = {
            "allow": True, "reason": "FIRST_OBSERVED_THESIS",
            "old_thesis_id": None, "new_thesis_id": thesis.get("thesis_id"),
            "reset_event": None, "reset_timestamp": None,
            "causal_evidence": [], "rejected_evidence": [],
        }
    causal_levels = [
        item for item in level_snapshot["levels"] if item["causal_at_decision"]
    ]
    target_shadow.update({
        "nearest_causal_opportunity": target_shadow.get("nearest_checkpoint"),
        "strongest_causal_opportunity": max(
            (item for item in causal_levels if "OPPORTUNITY" in item["role_candidate"]),
            key=lambda item: float(item.get("strength") or 0.0), default=None,
        ),
        "farthest_defensible_causal_opportunity": max(
            (item for item in causal_levels if "OPPORTUNITY" in item["role_candidate"]),
            key=lambda item: abs(float(item["price"]) - entry), default=None,
        ),
        "v1_final_tp": target,
        "v1_final_tp_r": abs(target - entry) / abs(entry - stop),
        "v1_target_is_market_derived": str(target_plan.get("source") or "") not in {
            "", "fallback_rr", "nearest_liquidity_checkpoint_only"
        },
        "v1_target_matches_known_level": _target_matches_level(
            target, entry, float(stop_plan.get("tick_size") or 0.0), causal_levels
        ),
        "synthetic_terminal_target": None,
        "valid_for_research": market_integrity["valid_for_research"],
    })
    stop_shadow["valid_for_research"] = bool(
        market_integrity["valid_for_research"]
        and ta_integrity["valid_for_research"]
        and thesis_integrity["valid_for_research"]
    )
    reentry_shadow["valid_for_research"] = bool(
        ta_integrity["valid_for_research"]
        and reset_integrity["valid_for_research"]
    )
    decision = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "record_type": "STRATEGY_V2_SHADOW_DECISION",
        "execution_authority": False,
        "proposal_id": proposal_copy.get("proposal_id"),
        "trade_id": proposal_copy.get("proposal_id"),
        "symbol": proposal_copy.get("symbol"),
        "side": side,
        "decision_timestamp": _iso(decision_at),
        "latest_allowed_market_timestamp": aggregate_integrity["latest_source_timestamp"],
        "data_integrity": aggregate_integrity,
        "what_v1_did": {
            "decision": "AUTHORITATIVE_PROPOSAL",
            "entry": entry, "stop": stop, "target": target,
            "amount": proposal_copy.get("amount"),
            "target_plan": target_plan,
        },
        "causal_level_map": level_snapshot["levels"],
        "thesis": thesis,
        "reentry_reset_evidence": copy.deepcopy(list(reset_evidence or [])),
        "what_v2_would_have_done": {
            "target": target_shadow,
            "stop": stop_shadow,
            "reentry": reentry_shadow,
            "management_policy_status": "REJECTED_NOT_DEPLOY_CANDIDATE",
        },
    }
    identity = copy.deepcopy(decision)
    decision["decision_record_hash"] = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest().upper()
    return decision


def build_management_shadow_record(
    *, symbol: str, trade_id: str, side: str, entry: float, initial_stop: float,
    current_v1_stop: float, best_price: float, atr: float, tick_size: float,
    confirmed_structure: Mapping[str, Any] | None, observed_at: Any,
    last_structure_at: Any = None, activation_r: float = 1.5,
) -> dict[str, Any]:
    risk = abs(float(entry) - float(initial_stop))
    if risk <= 0:
        raise ValueError("initial risk must be positive")
    side_l = str(side).lower()
    best_r = (
        (float(best_price) - float(entry)) / risk
        if side_l == "buy" else (float(entry) - float(best_price)) / risk
    )
    observed = _utc(observed_at)
    structure = dict(confirmed_structure or {})
    structure_at = _utc(structure.get("confirmed_at"))
    structure_timestamp_field = "confirmed_at" if structure.get("confirmed_at") not in (None, "") else None
    previous_structure_at = _utc(last_structure_at)
    management_violations = []
    if observed is None:
        management_violations.append({
            "component": "management_integrity",
            "reason": "MISSING_OBSERVED_AT", "timestamp": "",
        })
    if structure_at is None:
        management_violations.append({
            "component": "management_integrity",
            "reason": "MISSING_SOURCE_TIMESTAMP", "timestamp": "",
        })
    elif observed and structure_at > observed:
        management_violations.append({
            "component": "management_integrity",
            "reason": "LOOKAHEAD_SOURCE_TIMESTAMP",
            "timestamp": _iso(structure_at),
        })
    if last_structure_at not in (None, "") and previous_structure_at is None:
        management_violations.append({
            "component": "management_integrity",
            "reason": "MISSING_LAST_STRUCTURE_TIMESTAMP", "timestamp": "",
        })
    elif observed and previous_structure_at and previous_structure_at > observed:
        management_violations.append({
            "component": "management_integrity",
            "reason": "LOOKAHEAD_LAST_STRUCTURE_TIMESTAMP",
            "timestamp": _iso(previous_structure_at),
        })
    management_valid = not management_violations
    if management_valid:
        verdict = structural_management_candidate(
            side=side_l, entry=float(entry), current_stop=float(current_v1_stop),
            initial_risk=risk, best_r=best_r, activation_r=float(activation_r),
            atr=float(atr), tick_size=float(tick_size),
            confirmed_structure=structure,
            last_structure_at=last_structure_at,
        )
    else:
        verdict = {
            "move": False,
            "new_stop": None,
            "market_information": None,
            "structure_confirmed_at": _iso(structure_at),
            "buffer": None,
            "reason": "INVALID_CAUSAL_MANAGEMENT_EVIDENCE",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "record_type": "STRATEGY_V2_MANAGEMENT_SHADOW",
        "execution_authority": False,
        "trade_id": trade_id, "symbol": symbol, "side": side_l,
        "timestamp": _iso(observed),
        "old_stop": float(current_v1_stop),
        "candidate_stop": verdict.get("new_stop"),
        "market_information": verdict.get("market_information"),
        "structure_id": structure.get("structure_id"),
        "structure_confirmed_at": verdict.get("structure_confirmed_at"),
        "structure_timestamp_field": structure_timestamp_field,
        "atr_buffer": verdict.get("buffer"),
        "atr": float(atr), "r_at_decision": best_r,
        "shadow_decision": "MEANINGFUL_MOVE" if verdict.get("move") else "NO_MOVE",
        "reason": verdict.get("reason"),
        "valid_for_research": management_valid,
        "management_integrity": {
            "observed_at_valid": observed is not None,
            "structure_confirmed_at_present": structure_at is not None,
            "structure_confirmed_at_lte_observed_at": bool(
                observed and structure_at and structure_at <= observed
            ),
            "last_structure_at_lte_observed_at": bool(
                last_structure_at in (None, "")
                or (observed and previous_structure_at and previous_structure_at <= observed)
            ),
            "integrity_violations": management_violations,
        },
        "v1_stop_unchanged": True,
    }


def collect_authoritative_proposal_evidence(
    *, storage, proposal: Mapping[str, Any], coin_data: Mapping[str, Any],
    reset_evidence: Iterable[Mapping[str, Any]] | None = None,
    audit_ids: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist/audit shadow evidence; never return a V1 control decision."""
    context = copy.deepcopy(dict(coin_data.get("_ta_context") or {}))
    shadow_sources = dict(coin_data.get("_strategy_v2_shadow_sources") or {})
    for timeframe, source_key in (("4h", "context_4h"), ("1h", "context_1h")):
        source = context.get(source_key) or coin_data.get(source_key)
        if isinstance(source, Mapping) and source:
            context[timeframe] = copy.deepcopy(dict(source))
    if isinstance(shadow_sources.get("15m"), Mapping):
        context["15m"] = copy.deepcopy(dict(shadow_sources["15m"]))
    confirmation_bar = proposal.get("confirmation_bar_id")
    confirmation_open = _utc(confirmation_bar)
    if confirmation_bar not in (None, ""):
        context["5m"] = {
            "bar_id": str(confirmation_bar),
            "bar_open_at": _iso(confirmation_open),
            "confirmed_at": _iso(
                confirmation_open + timedelta(minutes=5)
                if confirmation_open else None
            ),
            "source": "USER_VISIBLE_TRADE_PROPOSAL.confirmation_bar_id",
        }
    prior = storage.get_latest_closed_thesis(
        str(proposal.get("symbol") or ""), str(proposal.get("side") or "")
    )
    record = build_shadow_decision_record(
        proposal=proposal, market_levels=coin_data.get("market_levels") or {},
        ta_context=context, reset_evidence=reset_evidence,
        prior_closed_thesis=prior,
    )
    if not storage.save_strategy_v2_shadow_decision(record):
        raise RuntimeError("SHADOW_DECISION_PERSISTENCE_FAILED")
    from utils.decision_audit import audit_decision_event
    audit_decision_event(
        **dict(audit_ids or {}),
        stage="STRATEGY_V2_SHADOW_DECISION",
        status=(
            "VALID_CAUSAL_SNAPSHOT" if record["data_integrity"]["valid_for_research"]
            else "INVALID_RESEARCH_SAMPLE"
        ),
        details=record,
        durable=True,
    )
    return record


def collect_closed_outcome_evidence(
    *, storage, proposal_id: str, trade_id: str, symbol: str,
    exit_details: Mapping[str, Any], audit_ids: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    outcome = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "STRATEGY_V2_SHADOW_OUTCOME",
        "execution_authority": False,
        "proposal_id": proposal_id, "trade_id": trade_id, "symbol": symbol,
        "recorded_at": _iso(datetime.now(timezone.utc)),
        "realized_R": _number(exit_details.get("final_r")) or 0.0,
        "MFE_R": _number(exit_details.get("mfe_r")) or 0.0,
        "MAE_R": _number(exit_details.get("mae_r")) or 0.0,
        "fees": _number(exit_details.get("commission")) or 0.0,
        "funding": _number(exit_details.get("funding")) or 0.0,
        "post_exit_MFE": {},
        "decision_fields_accessible": False,
    }
    if not storage.record_strategy_v2_shadow_outcome(proposal_id, outcome):
        raise RuntimeError("SHADOW_OUTCOME_PERSISTENCE_FAILED")
    from utils.decision_audit import audit_decision_event
    audit_decision_event(
        **dict(audit_ids or {}), stage="STRATEGY_V2_SHADOW_OUTCOME",
        status="CLOSED_OUTCOME_ATTACHED", details=outcome, durable=True,
    )
    return outcome


def collect_post_exit_checkpoint(
    *, storage, trade_id: str, horizon: str, checkpoint: Mapping[str, Any],
) -> bool:
    return bool(storage.update_strategy_v2_shadow_post_exit(
        trade_id, str(horizon), copy.deepcopy(dict(checkpoint))
    ))
