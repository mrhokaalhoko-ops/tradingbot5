"""Causal, closed-candle features for the frozen R4 Shadow policies.

This module is intentionally pure: it has no network, account, or execution
dependency.  Every level is calculated from candles whose close time is no
later than the immutable market snapshot timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import Candle, CandleOpenEvent, ExecutionQuote, Geometry, MarketSnapshot


def ema(values: Iterable[float], period: int) -> list[float]:
    rows = [float(value) for value in values]
    if not rows:
        return []
    alpha = 2.0 / (float(period) + 1.0)
    result = [rows[0]]
    for value in rows[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def atr(candles: Iterable[Candle], period: int = 14) -> float | None:
    rows = list(candles)
    if len(rows) < period + 1:
        return None
    ranges: list[float] = []
    for previous, current in zip(rows[:-1], rows[1:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return sum(ranges[-period:]) / float(period)


def confirmed_pivots(candles: Iterable[Candle], left: int = 2, right: int = 2) -> dict[str, list[dict]]:
    """Return pivots confirmed by fully closed bars on both sides."""
    rows = list(candles)
    highs: list[dict] = []
    lows: list[dict] = []
    for index in range(left, len(rows) - right):
        row = rows[index]
        window = rows[index - left:index + right + 1]
        if row.high == max(item.high for item in window) and sum(
            item.high == row.high for item in window
        ) == 1:
            highs.append({"id": row.candle_id, "price": row.high, "candle": row})
        if row.low == min(item.low for item in window) and sum(
            item.low == row.low for item in window
        ) == 1:
            lows.append({"id": row.candle_id, "price": row.low, "candle": row})
    return {"highs": highs, "lows": lows}


def _pick_supports(pivots: list[dict], price: float) -> tuple[dict | None, dict | None]:
    eligible = sorted((row for row in pivots if row["price"] < price), key=lambda row: row["price"], reverse=True)
    return (eligible[0] if eligible else None, eligible[1] if len(eligible) > 1 else (eligible[0] if eligible else None))


def _pick_resistances(pivots: list[dict], price: float) -> tuple[dict | None, dict | None]:
    eligible = sorted((row for row in pivots if row["price"] > price), key=lambda row: row["price"])
    return (eligible[0] if eligible else None, eligible[1] if len(eligible) > 1 else (eligible[0] if eligible else None))


def build_level_map(snapshot: MarketSnapshot) -> dict[str, Any]:
    rows = snapshot.closed("4h")
    price = snapshot.quote.best_ask
    pivots = confirmed_pivots(rows)
    near_support, main_support = _pick_supports(pivots["lows"], price)
    near_resistance, main_resistance = _pick_resistances(pivots["highs"], price)

    demand = None
    if near_support:
        candle = near_support["candle"]
        demand = {
            "id": f"demand:{candle.candle_id}",
            "lower": candle.low,
            "upper": max(candle.open, candle.close),
        }
    supply = None
    if near_resistance:
        candle = near_resistance["candle"]
        supply = {
            "id": f"supply:{candle.candle_id}",
            "lower": min(candle.open, candle.close),
            "upper": candle.high,
        }

    atr4h = atr(rows)
    cluster_tolerance = 0.15 * atr4h if atr4h else 0.0
    liquidity_below = None
    if near_support:
        cluster = [row for row in pivots["lows"] if abs(row["price"] - near_support["price"]) <= cluster_tolerance]
        liquidity_below = {
            "id": "liq_below:" + "+".join(row["id"] for row in cluster),
            "price": min(row["price"] for row in cluster),
        }
    liquidity_above = None
    if near_resistance:
        cluster = [row for row in pivots["highs"] if abs(row["price"] - near_resistance["price"]) <= cluster_tolerance]
        liquidity_above = {
            "id": "liq_above:" + "+".join(row["id"] for row in cluster),
            "price": max(row["price"] for row in cluster),
        }

    invalidation_values = []
    if near_support:
        invalidation_values.append(float(near_support["price"]))
    if demand:
        invalidation_values.append(float(demand["lower"]))
    return {
        "main_support": _level(main_support),
        "near_support": _level(near_support),
        "main_resistance": _level(main_resistance),
        "near_resistance": _level(near_resistance),
        "demand": demand,
        "supply": supply,
        "liquidity_above": liquidity_above,
        "liquidity_below": liquidity_below,
        "invalidation": min(invalidation_values) if invalidation_values else None,
        "atr4h": atr4h,
        "built_from_candle_id": rows[-1].candle_id if rows else None,
    }


def _level(row: dict | None) -> dict | None:
    if not row:
        return None
    return {"id": row["id"], "price": float(row["price"])}


def _long_bias(rows: tuple[Candle, ...], minimum: int, include_ema50: bool) -> bool:
    if len(rows) < minimum:
        return False
    closes = [row.close for row in rows]
    ema20 = ema(closes, 20)
    if include_ema50:
        ema50 = ema(closes, 50)
        return closes[-1] > ema20[-1] > ema50[-1]
    return closes[-1] > ema20[-1] and ema20[-1] > ema20[-4]


def _rising_structure(rows: tuple[Candle, ...]) -> bool:
    pivots = confirmed_pivots(rows)
    return (
        len(pivots["highs"]) >= 2
        and len(pivots["lows"]) >= 2
        and pivots["highs"][-1]["price"] > pivots["highs"][-2]["price"]
        and pivots["lows"][-1]["price"] > pivots["lows"][-2]["price"]
    )


def top_down_observation(snapshot: MarketSnapshot, level_map: dict[str, Any] | None = None) -> dict[str, Any]:
    levels = level_map or build_level_map(snapshot)
    weekly = snapshot.closed("1w")
    daily = snapshot.closed("1d")
    four_h = snapshot.closed("4h")
    hourly = snapshot.closed("1h")
    trigger = snapshot.closed("15m")
    required = {"1w": 24, "1d": 60, "4h": 80, "1h": 80, "15m": 80}
    missing = [name for name, minimum in required.items() if len(snapshot.closed(name)) < minimum]
    weekly_ok = _long_bias(weekly, 24, False)
    daily_ok = _long_bias(daily, 60, True)
    structure_ok = len(four_h) >= 80 and _rising_structure(four_h)
    atr4h = levels.get("atr4h")
    near_support = (levels.get("near_support") or {}).get("price")
    demand = levels.get("demand")
    invalidation = levels.get("invalidation")
    no_location = near_support is None and demand is None
    too_extended = bool(
        atr4h and near_support is not None
        and snapshot.quote.best_ask - near_support > 1.50 * atr4h
    )
    recovered = bool(
        atr4h and near_support is not None
        and snapshot.quote.best_ask - near_support <= 1.25 * atr4h
    )

    pullback = False
    breakout = False
    zone_tolerance = None
    if len(hourly) >= 2 and invalidation is not None:
        atr1h = atr(hourly)
        zone_tolerance = max(0.25 * atr1h, 0.002 * hourly[-1].close) if atr1h else 0.002 * hourly[-1].close
        locations = [value for value in (
            near_support,
            (demand or {}).get("upper") if demand else None,
        ) if value is not None]
        pullback = any(hourly[-1].low <= value + zone_tolerance for value in locations) and hourly[-1].close > invalidation
        resistance = (levels.get("near_resistance") or {}).get("price")
        breakout = bool(resistance and hourly[-2].close <= resistance < hourly[-1].close)

    trigger_ok = False
    if len(trigger) >= 2:
        current, previous = trigger[-1], trigger[-2]
        trigger_ok = current.close > current.open and current.close > previous.high

    return {
        "missing_timeframes": missing,
        "weekly_long": weekly_ok,
        "daily_long": daily_ok,
        "structure_4h_long": structure_ok,
        "location_available": not no_location and invalidation is not None,
        "bias_structure_location_valid": weekly_ok and daily_ok and structure_ok and not no_location and invalidation is not None,
        "closed_4h_below_invalidation": bool(four_h and invalidation is not None and four_h[-1].close < invalidation),
        "too_extended": too_extended,
        "recovered_from_extension": recovered,
        "pullback_at_zone": pullback,
        "breakout_1h": breakout,
        "trigger_15m": trigger_ok,
        "zone_tolerance": zone_tolerance,
        "level_map": levels,
    }


def shared_repriced_geometry(
    snapshot: MarketSnapshot,
    level_map: dict[str, Any],
    execution_quote: ExecutionQuote | None = None,
) -> tuple[Geometry | None, str]:
    """Build B/C geometry from the first fresh quote *after* ENTRY_READY."""
    if execution_quote is None:
        return None, "WAITING_FRESH_EXECUTION_QUOTE"
    atr5 = atr(snapshot.closed("5m"))
    if not atr5 or atr5 <= 0:
        return None, "SKIP_NO_ATR5M"
    entry = execution_quote.best_ask * 1.0001
    stop_candidates: list[tuple[str, str, float]] = []
    event_low = snapshot.candidate.event.get("low")
    if event_low:
        stop_candidates.append(("event_low", "EVENT_LOW_MINUS_0_10_ATR5M", float(event_low) - 0.10 * atr5))
    near_support = level_map.get("near_support") or {}
    if near_support.get("price"):
        stop_candidates.append((str(near_support.get("id")), "NEAR_SUPPORT_MINUS_0_10_ATR5M", float(near_support["price"]) - 0.10 * atr5))
    demand = level_map.get("demand") or {}
    if demand.get("lower"):
        stop_candidates.append((str(demand.get("id")), "DEMAND_LOWER_MINUS_0_10_ATR5M", float(demand["lower"]) - 0.10 * atr5))
    liquidity = level_map.get("liquidity_below") or {}
    if liquidity.get("price"):
        stop_candidates.append((str(liquidity.get("id")), "LIQUIDITY_BELOW_MINUS_0_10_ATR5M", float(liquidity["price"]) - 0.10 * atr5))
    valid_stops = [row for row in stop_candidates if 0.5 * atr5 <= entry - row[2] <= 2.0 * atr5]
    if not valid_stops:
        return None, "SKIP_NO_CAUSAL_STOP"
    stop_id, stop_source, stop = max(valid_stops, key=lambda row: row[2])

    target_candidates: list[tuple[str, str, float]] = []
    for key, source, field in (
        ("near_resistance", "NEAR_RESISTANCE", "price"),
        ("supply", "SUPPLY_LOWER", "lower"),
        ("liquidity_above", "LIQUIDITY_ABOVE", "price"),
    ):
        row = level_map.get(key) or {}
        if row.get(field):
            target_candidates.append((str(row.get("id")), source, float(row[field])))
    day = snapshot.closed("1h")[-24:]
    if day:
        target_candidates.append((f"prev24h:{day[0].candle_id}", "PREVIOUS_24H_HIGH", max(row.high for row in day)))
    valid_targets = [row for row in target_candidates if row[2] > entry]
    if not valid_targets:
        return None, "SKIP_NO_CAUSAL_TARGET"
    target_id, target_source, target = min(valid_targets, key=lambda row: row[2])
    risk = entry - stop
    reward = target - entry
    actual_rr = reward / risk if risk > 0 else 0.0
    if actual_rr < 1.5:
        return None, "SKIP_REPRICED_RR"
    return Geometry(
        executable_quote_time=execution_quote.event_time,
        executable_entry=entry,
        entry_quote_side="BEST_ASK",
        entry_price_source="BEST_ASK_PLUS_1_BPS_ADVERSE_SLIPPAGE",
        old_reference_entry=float(snapshot.candidate.event.get("close") or 0) or None,
        stop=stop,
        stop_source=stop_source,
        stop_level_id=stop_id,
        target=target,
        target_source=target_source,
        target_level_id=target_id,
        planned_rr=actual_rr,
        actual_rr_at_entry=actual_rr,
        geometry_change_reason="REPRICED_AT_FIRST_EXECUTABLE_QUOTE_AFTER_ENTRY_READY",
    ), "ENTRY_GEOMETRY_VALID"


def active_altcoin_geometry(
    snapshot: MarketSnapshot,
    setup: dict[str, Any],
    candle_open: CandleOpenEvent | None = None,
) -> tuple[Geometry | None, str]:
    """Construct A geometry only from a prospectively captured 1m open."""
    confirmation = setup.get("confirmation_close_time")
    if not confirmation:
        return None, "WAITING_RECLAIM"
    if candle_open is None:
        return None, "WAITING_PROSPECTIVE_1M_OPEN"
    entry = float(candle_open.open_price)
    event_atr = float(snapshot.candidate.event.get("atr") or 0)
    extreme = float(setup.get("extreme") or 0)
    if min(entry, event_atr, extreme) <= 0:
        return None, "WAITING_NEXT_1M_OPEN"
    stop = extreme - 0.10 * event_atr
    risk = entry - stop
    if not 0.5 * event_atr <= risk <= 2.0 * event_atr:
        return None, "INVALID_RISK"
    target = entry + 1.5 * risk
    return Geometry(
        executable_quote_time=candle_open.open_time,
        executable_entry=entry,
        entry_quote_side="NEXT_1M_OPEN",
        entry_price_source="EXACT_NEXT_1M_OPEN_AFTER_CONFIRMED_RECLAIM",
        old_reference_entry=float(snapshot.candidate.event.get("close") or 0) or None,
        stop=stop,
        stop_source="PULLBACK_EXTREME_MINUS_0_10_EVENT_ATR",
        stop_level_id=str(setup.get("setup_id") or "A_PULLBACK_EXTREME"),
        target=target,
        target_source="FIXED_1_5R_FROZEN_A_POLICY",
        target_level_id="A_TARGET_1_5R",
        planned_rr=1.5,
        actual_rr_at_entry=1.5,
        geometry_change_reason="A_EXACT_NEXT_1M_OPEN_AFTER_RECLAIM",
    ), "ENTRY_GEOMETRY_VALID"
