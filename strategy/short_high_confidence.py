"""Closed-candle, side-specific gate for HYBRID SHORT Demo proposals.

This module does not detect signals and does not calculate trade geometry.  It
only decides whether an already emitted HYBRID SELL has enough existing
context/location evidence to execute now, should keep waiting, or must be
rejected.  LONG never calls this policy.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

import pandas as pd


POLICY_NAME = "SHORT_HIGH_CONFIDENCE_V1"
RESISTANCE_NEAR_ATR = 0.75
MIN_SUPPORT_ROOM_ATR = 1.0
MIN_SUPPORT_ROOM_PCT = 0.003


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _point(price: Any, source: str) -> dict[str, Any] | None:
    value = _number(price)
    if value <= 0:
        return None
    return {"low": value, "high": value, "source": source}


def _zone(value: Any, source: str) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    low = _number(value.get("low", value.get("price")))
    high = _number(value.get("high", value.get("price")))
    if low <= 0 or high <= 0:
        return None
    if low > high:
        low, high = high, low
    return {"low": low, "high": high, "source": source}


def _iter_zones(values: Any, source: str) -> Iterable[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        zone = _zone(item, f"{source}[{index}]")
        if zone is None and isinstance(item, Mapping):
            zone = _point(item.get("price"), f"{source}[{index}]")
        if zone is not None:
            result.append(zone)
    return result


def _nearest_resistance(price: float, candidates: Iterable[dict[str, Any]]):
    eligible = []
    for item in candidates:
        low, high = _number(item.get("low")), _number(item.get("high"))
        if high < price:
            continue
        distance = 0.0 if low <= price <= high else max(0.0, low - price)
        eligible.append((distance, {**item, "distance": distance}))
    return min(eligible, key=lambda pair: pair[0])[1] if eligible else None


def _nearest_support(price: float, candidates: Iterable[dict[str, Any]]):
    eligible = []
    for item in candidates:
        low, high = _number(item.get("low")), _number(item.get("high"))
        if low > price:
            continue
        distance = 0.0 if low <= price <= high else max(0.0, price - high)
        eligible.append((distance, {**item, "distance": distance}))
    return min(eligible, key=lambda pair: pair[0])[1] if eligible else None


def _distance_payload(level: Mapping[str, Any] | None, price: float, atr: float):
    if not level:
        return None
    distance = _number(level.get("distance"))
    return {
        "source": level.get("source"),
        "low": _number(level.get("low")),
        "high": _number(level.get("high")),
        "distance": distance,
        "distance_atr": round(distance / atr, 6) if atr > 0 else None,
        "distance_pct": round(distance / price, 8) if price > 0 else None,
    }


def _bar_id(value: Any) -> str:
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return str(int(timestamp.timestamp() * 1000))
    except Exception:
        return str(value)


def evaluate_short_high_confidence(
    *,
    decision_price: float,
    atr: float,
    trend_1h: str,
    context_4h: Mapping[str, Any] | None,
    context_1h: Mapping[str, Any] | None,
    market_levels: Mapping[str, Any] | None,
    trigger_bars: pd.DataFrame | None,
    invalidation_level: float | None = None,
) -> dict[str, Any]:
    """Evaluate an existing HYBRID SELL using only already available inputs."""

    price = _number(decision_price)
    atr = _number(atr)
    trend_1h = str(trend_1h or "unknown").strip().lower()
    context_4h = dict(context_4h or {})
    context_1h = dict(context_1h or {})
    levels = dict(market_levels or {})
    trend_4h = str(context_4h.get("trend") or "UNKNOWN").upper()

    result: dict[str, Any] = {
        "policy": POLICY_NAME,
        "status": "WAIT_SHORT_CONFIRMATION",
        "reason": "SHORT_WAIT_CONFIRMATION",
        "decision_price": price,
        "atr": atr,
        "trend_1h": trend_1h.upper(),
        "trend_4h": trend_4h,
        "nearest_resistance_supply": None,
        "nearest_support_demand": None,
        "trigger": {},
        "closed_5m_only": True,
    }
    if price <= 0 or atr <= 0:
        result.update(status="REJECTED", reason="SHORT_REJECT_DATA")
        return result

    if trend_4h == "BULLISH" or (
        trend_1h == "bullish" and trend_4h != "BEARISH"
    ):
        result.update(status="REJECTED", reason="SHORT_REJECT_HTF_BULLISH")
        return result
    if trend_4h not in {"BEARISH", "RANGE"} or trend_1h not in {
        "bearish", "neutral"
    }:
        result.update(reason="SHORT_WAIT_HTF_CONTEXT")
        return result

    resistance_candidates: list[dict[str, Any]] = []
    support_candidates: list[dict[str, Any]] = []
    resistance_candidates.extend(_iter_zones(levels.get("swing_highs"), "swing_high"))
    resistance_candidates.extend(_iter_zones(levels.get("bearish_obs"), "bearish_ob"))
    support_candidates.extend(_iter_zones(levels.get("swing_lows"), "swing_low"))
    support_candidates.extend(_iter_zones(levels.get("bullish_obs"), "bullish_ob"))
    for value, source, target in (
        (context_4h.get("nearest_resistance_zone"), "4h_resistance", resistance_candidates),
        (context_4h.get("nearest_support_zone"), "4h_support", support_candidates),
    ):
        item = _zone(value, source)
        if item:
            target.append(item)
    for value, source, target in (
        (context_1h.get("resistance"), "1h_resistance", resistance_candidates),
        (context_1h.get("support"), "1h_support", support_candidates),
    ):
        item = _point(value, source)
        if item:
            target.append(item)

    nearest_resistance = _nearest_resistance(price, resistance_candidates)
    nearest_support = _nearest_support(price, support_candidates)
    result["nearest_resistance_supply"] = _distance_payload(
        nearest_resistance, price, atr
    )
    result["nearest_support_demand"] = _distance_payload(
        nearest_support, price, atr
    )

    if nearest_support:
        minimum_room = max(MIN_SUPPORT_ROOM_ATR * atr, MIN_SUPPORT_ROOM_PCT * price)
        if _number(nearest_support.get("distance")) < minimum_room:
            result.update(
                status="REJECTED",
                reason="SHORT_REJECT_SUPPORT_TOO_CLOSE",
                minimum_support_room={
                    "price": minimum_room,
                    "atr": MIN_SUPPORT_ROOM_ATR,
                    "pct": MIN_SUPPORT_ROOM_PCT,
                },
            )
            return result

    invalidation = _number(invalidation_level)
    if invalidation > 0 and price >= invalidation:
        result.update(status="REJECTED", reason="SHORT_REJECT_STRUCTURAL_INVALIDATION")
        return result

    if trigger_bars is None or len(trigger_bars) < 15:
        result.update(reason="SHORT_WAIT_5M_DATA")
        return result
    required = {"open", "high", "low", "close"}
    if not required.issubset(trigger_bars.columns):
        result.update(reason="SHORT_WAIT_5M_DATA")
        return result

    bars = trigger_bars.copy()
    for column in required:
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    closed = bars.iloc[-2]
    history = bars.iloc[:-2].tail(12)
    open_price = _number(closed.get("open"))
    high = _number(closed.get("high"))
    low = _number(closed.get("low"))
    close = _number(closed.get("close"))
    candle_range = max(0.0, high - low)
    body = abs(close - open_price)
    upper_wick = max(0.0, high - max(open_price, close))
    bearish_close = close > 0 and close < open_price
    close_location = (close - low) / candle_range if candle_range > 0 else 0.5
    rejection_wick = upper_wick >= max(body, candle_range * 0.20)

    resistance_distance = (
        _number(nearest_resistance.get("distance")) if nearest_resistance else math.inf
    )
    near_resistance = resistance_distance <= RESISTANCE_NEAR_ATR * atr
    resistance_floor = _number(nearest_resistance.get("low")) if nearest_resistance else 0.0
    directional_failure = bool(
        bearish_close
        and close_location <= 0.50
        and (resistance_floor <= 0 or close < resistance_floor)
    )
    rejection_trigger = bool(
        near_resistance and bearish_close and rejection_wick and directional_failure
    )

    prior_support = _number(history["low"].min()) if not history.empty else 0.0
    breakdown_retest = bool(
        prior_support > 0
        and bearish_close
        and low < prior_support
        and close < prior_support
        and high >= prior_support
    )
    bar_id = _bar_id(closed.get("timestamp", closed.get("time", closed.name)))
    result["trigger"] = {
        "bar_id": bar_id,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "bearish_close": bearish_close,
        "close_location": round(close_location, 6),
        "upper_wick": upper_wick,
        "rejection_wick": rejection_wick,
        "near_resistance": near_resistance,
        "directional_failure": directional_failure,
        "prior_support": prior_support,
        "breakdown_retest": breakdown_retest,
        "volume_required": False,
        "sweep_required": False,
    }

    if rejection_trigger:
        result.update(
            status="PASS",
            reason="SHORT_PASS_RESISTANCE_REJECTION",
            trigger_pattern="RESISTANCE_REJECTION",
        )
    elif breakdown_retest:
        result.update(
            status="PASS",
            reason="SHORT_PASS_BREAKDOWN_RETEST",
            trigger_pattern="BREAKDOWN_RETEST",
        )
    elif nearest_resistance is None:
        result.update(status="REJECTED", reason="SHORT_REJECT_LOCATION")
    elif not near_resistance:
        result.update(status="REJECTED", reason="SHORT_REJECT_LOCATION")
    else:
        result.update(reason="SHORT_REJECT_5M_WAITING")
    return result
