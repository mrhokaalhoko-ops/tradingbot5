"""Unified, look-ahead-free entry confirmation for Scanner and Sniper.

The module deliberately separates the 15m setup from the 5m execution trigger.
It does not try to predict price.  It verifies that the original structural
thesis still has market room and that a *closed* 5m candle confirms the entry
location.  Both execution paths call the same function, so proximity to the
planned entry can never bypass confirmation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from strategy.market_regime import STRONG_RISK_OFF, STRONG_RISK_ON


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _level_token(value: Any) -> str:
    """Stable token for prices without making the thesis candle-dependent."""
    number = _finite(value)
    return f"{number:.10g}" if number else "0"


def _time_token(value: Any) -> str:
    if value in (None, ""):
        return "unknown"
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return str(int(timestamp.timestamp() * 1000))
    except Exception:
        return str(value)


def build_setup_thesis_id(
    symbol: str,
    side: str,
    ta_context: dict | None,
    profile_key: str,
) -> str:
    """Return a stable identity for one structural setup.

    The anchor and structural levels remain unchanged while the same OB thesis
    is being recycled.  A genuinely new OB/BOS creates a different identity and
    is therefore not blocked by an old setup cooldown.
    """
    context = ta_context or {}
    anchor = context.get("setup_anchor") or {}
    payload = {
        "symbol": str(symbol or "").upper().split(":")[0],
        "side": str(side or "").lower(),
        "profile": str(profile_key or ""),
        "anchor": _time_token(anchor.get("created_at") or anchor.get("index")),
        "zone_low": _level_token(anchor.get("low") or context.get("ob_low")),
        "zone_high": _level_token(anchor.get("high") or context.get("ob_high")),
        "entry": _level_token(context.get("optimal_entry")),
        "invalidation": _level_token(context.get("invalidation_level")),
        "structure_side": str(anchor.get("type") or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"setup_{hashlib.sha256(encoded).hexdigest()[:20]}"


def entry_zone_tolerance(entry: float, atr_15m: float) -> float:
    """Adaptive entry tolerance: 0.15%-0.50%, scaled by 15m ATR."""
    entry = _finite(entry)
    atr_15m = _finite(atr_15m)
    if entry <= 0:
        return 0.0
    return max(entry * 0.0015, min(atr_15m * 0.25, entry * 0.005))


def classify_virtual_invalidation(
    *,
    side: str,
    current_price: float,
    invalidation_level: float,
    prior_state: str = "VALID",
    closed_price: float | None = None,
    closed_after_breach: bool = False,
) -> str:
    """Separate an intrabar liquidity probe from structural invalidation.

    A live-price breach has no authority to kill a 15m thesis. It becomes
    ``SWEEP_PENDING``. Only the first 15m candle that closes after that probe
    can classify it as a reclaim or a hard invalidation.
    """
    side = str(side or "").lower()
    current_price = _finite(current_price)
    invalidation_level = _finite(invalidation_level)
    closed_price = _finite(closed_price)
    if side not in ("buy", "sell") or invalidation_level <= 0:
        return "VALID"

    tick_breached = (
        current_price <= invalidation_level
        if side == "buy"
        else current_price >= invalidation_level
    )
    if closed_after_breach and closed_price > 0:
        close_reclaimed = (
            closed_price > invalidation_level
            if side == "buy"
            else closed_price < invalidation_level
        )
        return "RECLAIM_CONFIRMED" if close_reclaimed else "HARD_INVALIDATED"
    if tick_breached or prior_state == "SWEEP_PENDING":
        return "SWEEP_PENDING"
    return "VALID"


def _timestamp_utc(value: Any) -> datetime | None:
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.to_pydatetime()
    except Exception:
        return None


def evaluate_entry_confirmation(
    *,
    side: str,
    trigger_bars: pd.DataFrame,
    current_price: float,
    optimal_entry: float,
    invalidation_level: float,
    atr_15m: float,
    ta_context: dict | None,
    filter_profile: dict | None,
    planned_rr: float | None,
    zone_touch_recorded: bool = False,
    now: datetime | None = None,
) -> dict:
    """Evaluate a closed-5m trigger against the existing 15m setup.

    ``score`` is an explanatory diagnostic, not an optimized prediction.  The
    actual decision is made by explicit independent gates (data, structure,
    location, reclaim, participation and market room).
    """
    profile = filter_profile or {}
    context = ta_context or {}
    side = str(side or "").lower()
    zone_touch_recorded = bool(zone_touch_recorded)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    result = {
        "passed": False,
        "score": 0,
        "reason": "",
        "bar_id": None,
        "closed_bar": None,
        "timeframe": "5m",
        "evidence": {},
        "failed_gates": [],
        # Capture the actual call inputs even when missing candles cause an
        # early return. This price is a decision reference, never proof of a fill.
        "inputs": {
            "decision_time": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "side": side,
            "reference_price": _finite(current_price, None),
            "price_role": "DECISION_REFERENCE_NOT_EXECUTABLE_QUOTE",
            "optimal_entry": _finite(optimal_entry, None),
            "invalidation_level": _finite(invalidation_level, None),
            "atr_15m": _finite(atr_15m, None),
            "planned_rr": _finite(planned_rr, None),
            "zone_touch_recorded": zone_touch_recorded,
            "context": {
                **{key: _finite(context.get(key), None) for key in
                   ("score", "ema_50", "ema_200", "adx")},
                "setup_anchor_present": bool(context.get("setup_anchor")),
                "htf_trend": str(context.get("htf_trend") or "unknown"),
                "market_regime": str((context.get("market_regime") or {}).get("label") or "UNKNOWN"),
                "invalidation_state": str(context.get("invalidation_state") or "VALID"),
            },
            "profile": {key: profile.get(key) for key in (
                "key", "require_ob", "min_rr", "trigger_evidence_min",
                "require_volume", "require_entry_sweep", "allow_countertrend_reversal",
            ) if key in profile},
        },
    }
    if side not in ("buy", "sell"):
        result["reason"] = "INVALID_SIDE"
        result["failed_gates"] = ["side"]
        return result
    if trigger_bars is None or len(trigger_bars) < 22:
        result["reason"] = "INSUFFICIENT_5M_DATA"
        result["failed_gates"] = ["data"]
        return result

    bars = trigger_bars.copy()
    for column in ("open", "high", "low", "close", "volume"):
        if column not in bars.columns:
            result["reason"] = f"MISSING_5M_COLUMN:{column}"
            result["failed_gates"] = ["data"]
            return result
        bars[column] = pd.to_numeric(bars[column], errors="coerce")

    # Binance REST/WS windows contain the live candle at [-1].  Only [-2] and
    # earlier rows are allowed to influence execution.
    closed = bars.iloc[-2]
    history = bars.iloc[:-2]
    previous_20 = history.tail(20)
    previous_12 = history.tail(12)

    open_price = _finite(closed.get("open"))
    high = _finite(closed.get("high"))
    low = _finite(closed.get("low"))
    close = _finite(closed.get("close"))
    volume = _finite(closed.get("volume"))
    current_price = _finite(current_price)
    optimal_entry = _finite(optimal_entry)
    invalidation_level = _finite(invalidation_level)
    planned_rr = _finite(planned_rr)
    min_rr = _finite(profile.get("min_rr"), 1.5)

    timestamp_value = closed.get("timestamp", closed.get("time", closed.name))
    opened_at = _timestamp_utc(timestamp_value)
    if opened_at is not None:
        closed_at = opened_at + timedelta(minutes=5)
        age_seconds = (now.astimezone(timezone.utc) - closed_at).total_seconds()
        expected_open_ms = (
            int(now.astimezone(timezone.utc).timestamp() * 1000) // (5 * 60 * 1000) - 1
        ) * (5 * 60 * 1000)
        observed_open_ms = int(opened_at.timestamp() * 1000)
        exact_closed_bar = observed_open_ms == expected_open_ms
        data_fresh = exact_closed_bar and -60 <= age_seconds <= 7 * 60
        bar_id = str(int(opened_at.timestamp() * 1000))
    else:
        age_seconds = None
        expected_open_ms = None
        exact_closed_bar = False
        data_fresh = False
        bar_id = str(timestamp_value)
    result["bar_id"] = bar_id
    result["closed_bar"] = {
        "closed": True,
        "open_time": opened_at.isoformat().replace("+00:00", "Z") if opened_at else None,
        "close_time": closed_at.isoformat().replace("+00:00", "Z") if opened_at else None,
        "decision_time": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "source": "BINANCE_USDM_FUTURES_REST",
        "expected_open_ms": expected_open_ms,
        "exact_closed_bar": exact_closed_bar,
    }

    candle_range = max(0.0, high - low)
    body = abs(close - open_price)
    effective_body = max(body, candle_range * 0.05)
    lower_wick = max(0.0, min(open_price, close) - low)
    upper_wick = max(0.0, high - max(open_price, close))
    close_location = (close - low) / candle_range if candle_range > 0 else 0.5

    average_volume = _finite(previous_20["volume"].mean())
    volume_ratio = volume / average_volume if average_volume > 0 else 0.0
    volume_confirmed = average_volume > 0 and volume_ratio >= 1.20

    prior_low = _finite(previous_12["low"].min())
    prior_high = _finite(previous_12["high"].max())
    if side == "buy":
        directional_close = close > open_price and close_location >= 0.60
        rejection = lower_wick >= max(effective_body, candle_range * 0.20)
        swept_liquidity = prior_low > 0 and low < prior_low and close > prior_low
        reclaim = close >= optimal_entry
        not_invalidated = invalidation_level <= 0 or close > invalidation_level
    else:
        directional_close = close < open_price and close_location <= 0.40
        rejection = upper_wick >= max(effective_body, candle_range * 0.20)
        swept_liquidity = prior_high > 0 and high > prior_high and close < prior_high
        reclaim = close <= optimal_entry
        not_invalidated = invalidation_level <= 0 or close < invalidation_level

    structural_sweep_reclaim = (
        str(context.get("invalidation_state") or "").upper()
        == "RECLAIM_CONFIRMED"
    )
    swept_liquidity = bool(swept_liquidity or structural_sweep_reclaim)

    tolerance = entry_zone_tolerance(optimal_entry, atr_15m)
    candle_touched_zone = low <= optimal_entry + tolerance and high >= optimal_entry - tolerance
    current_in_zone = bool(
        optimal_entry - tolerance
        <= current_price
        <= optimal_entry + tolerance
    )
    # قبل إنشاء أمر المراقبة يجب أن تكون شمعة التأكيد نفسها قد لمست المنطقة
    # وأن يبقى السعر اللحظي قريباً منها. بعد أن يسجل المراقب لمساً حقيقياً،
    # يصبح ذلك اللمس دليلاً دائماً داخل نافذة التأكيد ولا نطلب من الارتداد أن
    # يعود إلى المنطقة مرة أخرى كي نعترف بشمعة reclaim المغلقة.
    entry_location_valid = bool(
        zone_touch_recorded or (candle_touched_zone and current_in_zone)
    )

    htf_trend = str(context.get("htf_trend") or "unknown").lower()
    regime = context.get("market_regime") or {}
    regime_label = str(regime.get("label") or "UNKNOWN").upper()
    ema_50 = _finite(context.get("ema_50"))
    ema_200 = _finite(context.get("ema_200"))
    adx = _finite(context.get("adx"))
    setup_score = int(_finite(context.get("score")))
    require_ob = bool(profile.get("require_ob", True))
    structure_valid = bool(context.get("setup_anchor")) and setup_score >= 3
    if not require_ob:
        structure_valid = setup_score >= 2

    htf_known = htf_trend in ("bullish", "bearish", "neutral")
    htf_opposes = (
        (side == "buy" and htf_trend == "bearish")
        or (side == "sell" and htf_trend == "bullish")
    )
    htf_aligned = (
        (side == "buy" and htf_trend == "bullish")
        or (side == "sell" and htf_trend == "bearish")
    )
    local_trend_aligned = (
        (side == "buy" and ema_50 > ema_200 > 0)
        or (side == "sell" and 0 < ema_50 < ema_200)
    )
    countertrend_allowed = bool(profile.get("allow_countertrend_reversal", False))
    target_room = planned_rr >= min_rr and planned_rr > 0
    regime_opposes = (
        (side == "sell" and regime_label == STRONG_RISK_ON)
        or (side == "buy" and regime_label == STRONG_RISK_OFF)
    )

    participation_count = sum((bool(rejection), bool(swept_liquidity), bool(volume_confirmed)))
    evidence_required = max(0, int(profile.get("trigger_evidence_min", 1) or 0))
    require_volume = bool(profile.get("require_volume", False))
    require_entry_sweep = bool(profile.get("require_entry_sweep", False))
    participation_ok = (
        participation_count >= evidence_required
        and (volume_confirmed or not require_volume)
        and (swept_liquidity or not require_entry_sweep)
    )
    if htf_opposes:
        # Counter-trend is an exceptional reversal setup, never a relaxed path.
        participation_ok = rejection and swept_liquidity and volume_confirmed
    regime_policy = True
    if regime_opposes:
        # Relative weakness is not an absolute short in broad risk-on. The
        # inverse applies to longs in broad risk-off.
        regime_policy = (
            local_trend_aligned
            and rejection
            and swept_liquidity
            and volume_confirmed
        )

    gates = {
        "data_fresh": data_fresh,
        "structure_valid": structure_valid,
        "htf_known": htf_known,
        "htf_policy": (not htf_opposes) or countertrend_allowed,
        "regime_policy": regime_policy,
        "local_trend": local_trend_aligned,
        "target_room": target_room,
        "entry_location": entry_location_valid,
        "directional_reclaim": directional_close and reclaim,
        "not_invalidated": not_invalidated,
        "participation": participation_ok,
    }

    score = 0
    score += 20 if structure_valid else 0
    score += 10 if local_trend_aligned else 0
    score += 15 if htf_aligned else (7 if htf_trend == "neutral" else 0)
    score += 5 if adx >= 20 else 0
    score += 10 if entry_location_valid else 0
    score += 15 if directional_close and reclaim else 0
    score += 10 if rejection else 0
    score += 10 if swept_liquidity else 0
    score += 5 if volume_confirmed else 0

    failed = [name for name, passed in gates.items() if not passed]
    result.update({
        "passed": not failed,
        "score": score,
        "reason": "CONFIRMED" if not failed else "FAILED:" + ",".join(failed),
        "failed_gates": failed,
        "evidence": {
            "data_fresh": data_fresh,
            "bar_age_seconds": age_seconds,
            "structure_valid": structure_valid,
            "htf_trend": htf_trend,
            "htf_aligned": htf_aligned,
            "countertrend": htf_opposes,
            "market_regime": regime_label,
            "regime_opposes": regime_opposes,
            "regime_policy": regime_policy,
            "local_trend_aligned": local_trend_aligned,
            "adx": adx,
            "planned_rr": planned_rr,
            "min_rr": min_rr,
            "zone_tolerance_pct": tolerance / optimal_entry if optimal_entry > 0 else 0.0,
            "candle_touched_zone": candle_touched_zone,
            "current_in_zone": current_in_zone,
            "zone_touch_recorded": zone_touch_recorded,
            "entry_location_valid": entry_location_valid,
            "directional_close": directional_close,
            "reclaim": reclaim,
            "rejection": rejection,
            "swept_liquidity": swept_liquidity,
            "structural_sweep_reclaim": structural_sweep_reclaim,
            "volume_confirmed": volume_confirmed,
            "volume_ratio": round(volume_ratio, 4),
            "participation_count": participation_count,
            "participation_required": evidence_required,
            "require_volume": require_volume,
            "require_entry_sweep": require_entry_sweep,
            "not_invalidated": not_invalidated,
        },
    })
    return result
