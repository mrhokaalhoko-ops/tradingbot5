"""Pure, causal research primitives for Strategy V2.

The module intentionally has no exchange, Telegram, persistence, or network
dependencies.  Runtime callers must opt in through disabled-by-default feature
flags.  Replay uses the same functions so research and any future integration
cannot silently diverge.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math
from typing import Any, Iterable


TIMEFRAME_WEIGHT = {"1w": 50.0, "1d": 40.0, "4h": 30.0, "1h": 20.0, "15m": 10.0}
RESET_EVENTS = {
    "NEW_BOS",
    "NEW_CHOCH",
    "NEW_LIQUIDITY_SWEEP",
    "NEW_ORDER_BLOCK",
    "NEW_ENTRY_ZONE",
    "INVALIDATION_AND_RECLAIM",
    "MATERIAL_HTF_CONTEXT_CHANGE",
}


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timeframe(raw: Any, fallback: str = "15m") -> str:
    value = str(raw or fallback).strip().lower()
    return value if value in TIMEFRAME_WEIGHT else fallback


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    return [item for item in (value or []) if isinstance(item, dict)]


@dataclass(frozen=True)
class CausalLevel:
    price: float
    level_type: str
    timeframe: str
    created_at: str
    age: float | None
    strength: float
    distance_from_entry: float
    rr_from_actual_entry: float
    causal: bool
    causal_reason: str
    source: str
    thesis_linked: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iter_level_sources(market_levels: dict[str, Any]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Yield (timeframe, family, record), accepting flat or timeframe maps."""
    levels = market_levels or {}
    for key, value in levels.items():
        key_l = str(key).lower()
        if key_l in TIMEFRAME_WEIGHT and isinstance(value, dict):
            for family, members in value.items():
                for item in _records(members):
                    yield key_l, str(family), item
        else:
            for item in _records(value):
                yield _timeframe(item.get("timeframe")), str(key), item


def _candidate_price(side: str, family: str, item: dict[str, Any], *, target: bool) -> float | None:
    side = side.lower()
    family_l = family.lower()
    if target:
        prefer_high = side == "buy"
    else:
        prefer_high = side == "sell"
    keys = (
        ("high", "price", "level", "upper", "top")
        if prefer_high else
        ("low", "price", "level", "lower", "bottom")
    )
    if "previous_day_high" in family_l or "previous_week_high" in family_l:
        keys = ("price", "high", "level")
    elif "previous_day_low" in family_l or "previous_week_low" in family_l:
        keys = ("price", "low", "level")
    for key in keys:
        value = _number(item.get(key))
        if value:
            return value
    return None


def build_causal_level_map(
    *,
    side: str,
    entry: float,
    initial_stop: float,
    market_levels: dict[str, Any],
    decision_time: Any,
    target: bool,
) -> list[CausalLevel]:
    """Build an auditable level map without accepting future-created levels."""
    decision_at = _utc(decision_time)
    risk = abs(float(entry) - float(initial_stop))
    if risk <= 0:
        return []
    output: list[CausalLevel] = []
    for timeframe, family, item in _iter_level_sources(market_levels):
        price = _candidate_price(side, family, item, target=target)
        if not price:
            continue
        correct_side = price > entry if ((side.lower() == "buy") == target) else price < entry
        if not correct_side:
            continue
        created = _utc(item.get("created_at") or item.get("timestamp") or item.get("time"))
        causal = bool(decision_at and created and created <= decision_at)
        if not decision_at:
            reason = "MISSING_DECISION_TIME"
        elif not created:
            reason = "MISSING_LEVEL_TIMESTAMP"
        elif created > decision_at:
            reason = "FUTURE_LEVEL"
        else:
            reason = "KNOWN_BEFORE_DECISION"
        distance = abs(price - entry)
        strength = float(item.get("strength") or item.get("quality") or 0.0)
        strength += TIMEFRAME_WEIGHT.get(timeframe, 0.0)
        output.append(CausalLevel(
            price=price,
            level_type=family,
            timeframe=timeframe,
            created_at=created.isoformat() if created else "",
            age=float(item["age"]) if item.get("age") is not None else None,
            strength=strength,
            distance_from_entry=distance,
            rr_from_actual_entry=distance / risk,
            causal=causal,
            causal_reason=reason,
            source=str(item.get("source") or family),
            thesis_linked=bool(
                item.get("is_thesis_invalidation")
                or str(item.get("relation_to_thesis") or "").upper()
                in {"THESIS_INVALIDATION", "THESIS_STRUCTURE", "EXPLICIT_THESIS_INVALIDATION"}
            ),
        ))
    unique: dict[tuple[float, str, str], CausalLevel] = {}
    for level in output:
        key = (round(level.price, 12), level.level_type, level.timeframe)
        prior = unique.get(key)
        if prior is None or level.strength > prior.strength:
            unique[key] = level
    return sorted(unique.values(), key=lambda item: item.distance_from_entry)


def evaluate_causal_opportunity(
    *,
    side: str,
    entry: float,
    initial_stop: float,
    market_levels: dict[str, Any],
    decision_time: Any,
    minimum_rr: float,
) -> dict[str, Any]:
    """Evaluate market room; never manufacture a terminal R target."""
    levels = build_causal_level_map(
        side=side, entry=entry, initial_stop=initial_stop,
        market_levels=market_levels, decision_time=decision_time, target=True,
    )
    causal = [item for item in levels if item.causal]
    qualifying = [item for item in causal if item.rr_from_actual_entry >= minimum_rr]
    nearest = causal[0] if causal else None
    opportunity = max(qualifying, key=lambda item: item.rr_from_actual_entry) if qualifying else None
    return {
        "valid": bool(opportunity),
        "reason": "MARKET_DERIVED_OPPORTUNITY" if opportunity else "REJECT_NO_CAUSAL_MARKET_ROOM",
        "minimum_rr": float(minimum_rr),
        "nearest_checkpoint": nearest.to_dict() if nearest else None,
        "opportunity_limit": opportunity.to_dict() if opportunity else None,
        "levels": [item.to_dict() for item in levels],
        "synthetic_terminal_target": None,
        "fixed_final_tp": None,
    }


def select_thesis_invalidation(
    *,
    side: str,
    entry: float,
    setup_anchor: dict[str, Any] | None,
    explicit_invalidation: float | None,
    market_levels: dict[str, Any],
    decision_time: Any,
) -> dict[str, Any]:
    """Keep ENTRY_ANCHOR separate and choose a pre-entry thesis level."""
    anchor = dict(setup_anchor or {})
    decision_at = _utc(decision_time)
    side_l = side.lower()
    candidates: list[dict[str, Any]] = []

    anchor_price = _number(anchor.get("low" if side_l == "buy" else "high"))
    anchor_time = _utc(anchor.get("created_at"))
    if anchor_price:
        candidates.append({
            "price": anchor_price,
            "type": "ENTRY_ANCHOR",
            "source": f"setup_anchor:{anchor.get('type') or 'unknown'}",
            "timeframe": _timeframe(anchor.get("timeframe")),
            "created_at": anchor_time.isoformat() if anchor_time else "",
            "causal": bool(decision_at and anchor_time and anchor_time <= decision_at),
            "eligible": bool(anchor.get("is_thesis_invalidation", False)),
            "reject_reason": None if anchor.get("is_thesis_invalidation", False) else "ENTRY_ANCHOR_NOT_THESIS_INVALIDATION",
            "strength": 65.0,
        })

    explicit = _number(explicit_invalidation)
    if explicit:
        aliases_anchor = bool(anchor_price and abs(explicit - anchor_price) <= max(entry * 1e-8, 1e-12))
        candidates.append({
            "price": explicit,
            "type": "EXPLICIT_THESIS_INVALIDATION" if not aliases_anchor else "ENTRY_ANCHOR_ALIAS",
            "source": "explicit_invalidation",
            "timeframe": _timeframe(anchor.get("timeframe")),
            # This value is an input to the decision itself, so it is known at
            # decision time even when its originating candle was not retained.
            "created_at": anchor_time.isoformat() if anchor_time else (
                decision_at.isoformat() if decision_at else ""
            ),
            "causal": bool(
                decision_at and (
                    anchor_time is None or anchor_time <= decision_at
                )
            ),
            "eligible": not aliases_anchor or bool(anchor.get("is_thesis_invalidation", False)),
            "reject_reason": "ENTRY_ANCHOR_NOT_THESIS_INVALIDATION" if aliases_anchor and not anchor.get("is_thesis_invalidation", False) else None,
            "strength": 100.0,
        })

    structural = build_causal_level_map(
        side=side, entry=entry, initial_stop=(entry * (0.99 if side_l == "buy" else 1.01)),
        market_levels=market_levels, decision_time=decision_time, target=False,
    )
    for item in structural:
        candidates.append({
            "price": item.price,
            "type": "THESIS_INVALIDATION",
            "source": item.level_type,
            "timeframe": item.timeframe,
            "created_at": item.created_at,
            "causal": item.causal,
            "eligible": bool(item.causal and item.thesis_linked),
            "reject_reason": (
                item.causal_reason if not item.causal
                else (None if item.thesis_linked else "NOT_LINKED_TO_CURRENT_THESIS")
            ),
            "strength": item.strength,
        })
    eligible = [item for item in candidates if item["eligible"] and item["causal"]]
    if not eligible:
        return {
            "valid": False,
            "reason": "NO_VALID_THESIS_INVALIDATION",
            "entry_anchor": anchor_price,
            "selected": None,
            "candidates": candidates,
        }
    selected = max(
        eligible,
        key=lambda item: (
            item["strength"],
            -item["price"] if side_l == "buy" else item["price"],
        ),
    )
    return {
        "valid": True,
        "reason": "CAUSAL_THESIS_INVALIDATION_SELECTED",
        "entry_anchor": anchor_price,
        "selected": selected,
        "candidates": [
            item if item is selected else {
                **item,
                "reject_reason": item.get("reject_reason") or "LOWER_THESIS_RELEVANCE",
            }
            for item in candidates
        ],
    }


def evaluate_thesis_reentry(
    *,
    old_thesis_id: str | None,
    new_thesis_id: str | None,
    prior_closed_at: Any,
    reset_evidence: Iterable[dict[str, Any]] | None,
    decision_time: Any,
) -> dict[str, Any]:
    """Require a new, causal reset event; elapsed time is never sufficient."""
    old_id, new_id = str(old_thesis_id or ""), str(new_thesis_id or "")
    closed_at, decision_at = _utc(prior_closed_at), _utc(decision_time)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in reset_evidence or []:
        event = str(raw.get("event") or raw.get("type") or "").upper()
        event_at = _utc(raw.get("timestamp") or raw.get("created_at"))
        causal = bool(
            event in RESET_EVENTS and event_at and decision_at and event_at <= decision_at
            and (closed_at is None or event_at > closed_at)
        )
        record = {
            "event": event,
            "timestamp": event_at.isoformat() if event_at else "",
            "causal": causal,
            "evidence": raw,
        }
        (accepted if causal else rejected).append(record)
    same_identity = bool(old_id and new_id and old_id == new_id)
    if not same_identity and accepted:
        status = "ALLOW_NEW_THESIS_WITH_RESET"
        allow = True
    elif accepted:
        status = "ALLOW_RESET_THESIS"
        allow = True
    else:
        status = "REJECT_SAME_THESIS_REENTRY" if same_identity else "REJECT_UNPROVEN_THESIS_RESET"
        allow = False
    return {
        "allow": allow,
        "reason": status,
        "old_thesis_id": old_id or None,
        "new_thesis_id": new_id or None,
        "reset_event": accepted[0]["event"] if accepted else None,
        "reset_timestamp": accepted[0]["timestamp"] if accepted else None,
        "causal_evidence": accepted,
        "rejected_evidence": rejected,
    }


def structural_management_candidate(
    *,
    side: str,
    entry: float,
    current_stop: float,
    initial_risk: float,
    best_r: float,
    activation_r: float,
    atr: float,
    tick_size: float,
    confirmed_structure: dict[str, Any] | None,
    last_structure_at: Any = None,
    min_atr_improvement: float = 0.15,
    min_risk_improvement: float = 0.10,
    atr_buffer: float = 0.30,
) -> dict[str, Any]:
    """Return a move only for a newly confirmed structural progression."""
    if best_r < activation_r:
        return {"move": False, "reason": "WAIT_ACTIVATION", "new_stop": None}
    structure = dict(confirmed_structure or {})
    price = _number(structure.get("price"))
    confirmed_at = _utc(structure.get("confirmed_at") or structure.get("created_at"))
    last_at = _utc(last_structure_at)
    if not price or not confirmed_at:
        return {"move": False, "reason": "WAIT_CONFIRMED_STRUCTURE", "new_stop": None}
    if last_at and confirmed_at <= last_at:
        return {"move": False, "reason": "NO_NEW_MARKET_INFORMATION", "new_stop": None}
    side_l = side.lower()
    buffer = max(float(atr) * atr_buffer, float(tick_size) * 2.0)
    candidate = price - buffer if side_l == "buy" else price + buffer
    profitable = candidate >= entry if side_l == "buy" else candidate <= entry
    improves = candidate > current_stop if side_l == "buy" else candidate < current_stop
    min_improvement = max(
        float(tick_size) * 2.0,
        float(atr) * min_atr_improvement,
        float(initial_risk) * min_risk_improvement,
    )
    if not profitable:
        return {"move": False, "reason": "STRUCTURE_NOT_PROFIT_PROTECTING", "new_stop": None}
    if not improves:
        return {"move": False, "reason": "STOP_REGRESSION_OR_NO_IMPROVEMENT", "new_stop": None}
    if abs(candidate - current_stop) < min_improvement:
        return {"move": False, "reason": "HYSTERESIS_SKIP", "new_stop": None}
    return {
        "move": True,
        "reason": "NEW_CONFIRMED_STRUCTURE_PROGRESS",
        "new_stop": candidate,
        "structure_price": price,
        "structure_confirmed_at": confirmed_at.isoformat(),
        "buffer": buffer,
        "min_improvement": min_improvement,
        "market_information": structure.get("event") or structure.get("type") or "CONFIRMED_SWING",
    }
