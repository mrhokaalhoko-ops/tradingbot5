"""Causal fresh-plan continuation research path.

This module deliberately does not know how to place an exchange order.  It
turns a market-wide hot-list observation into a closed-5m setup, waits for a
later closed-5m hold/continuation candle, builds a completely fresh structural
plan, and scores its counterfactual outcome.  Keeping the research path pure
prevents a hot-list tag from becoming an accidental market-entry bypass.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


RISK_ON = {"STRONG_RISK_ON", "MILD_RISK_ON"}
RISK_OFF = {"STRONG_RISK_OFF", "MILD_RISK_OFF"}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _utc_timestamp(value: Any) -> pd.Timestamp | None:
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp
    except Exception:
        return None


def _closed_bars(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Return numeric closed bars, excluding the live REST candle at ``-1``."""
    if frame is None or len(frame) < 3:
        return pd.DataFrame()
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()
    closed = frame.iloc[:-1].copy()
    for column in required:
        closed[column] = pd.to_numeric(closed[column], errors="coerce")
    return closed.dropna(subset=list(required))


def _atr(closed: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = closed["close"].shift(1)
    true_range = pd.concat(
        [
            closed["high"] - closed["low"],
            (closed["high"] - previous_close).abs(),
            (closed["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period).mean()


def _bar_id(row: pd.Series) -> str:
    timestamp = _utc_timestamp(row.get("timestamp", row.get("time", row.name)))
    if timestamp is not None:
        return str(int(timestamp.timestamp() * 1000))
    return str(row.name)


def _stable_id(prefix: str, payload: dict) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:20]}"


def candidate_side(candidate: dict | None) -> str | None:
    """Choose one direction from hot-list momentum tags without guessing."""
    candidate = candidate or {}
    tags = {str(tag).upper() for tag in candidate.get("tags", [])}
    up = "MOMENTUM_UP" in tags
    down = "MOMENTUM_DOWN" in tags
    if up and not down:
        return "buy"
    if down and not up:
        return "sell"
    if up and down:
        change_5m = _finite(candidate.get("change_5m_pct"))
        change_1m = _finite(candidate.get("change_1m_pct"))
        direction = change_5m if change_5m else change_1m
        return "buy" if direction > 0 else "sell" if direction < 0 else None
    return None


def build_external_thesis_continuation_setup(
    *,
    symbol: str,
    side: str,
    source_thesis_id: str,
    bars_5m: pd.DataFrame,
    invalidation_level: float,
    market_regime: dict | None,
    context_15m: dict | None,
    source_snapshot: dict | None = None,
) -> dict:
    """Create a paired shadow continuation setup from an existing SMC thesis.

    The latest closed 5m price becomes a *new* continuation boundary.  A later
    closed bar must still confirm and a completely fresh stop/target plan must
    still pass.  The original SMC entry and target are evidence only; they are
    never reused by this alternative path.
    """
    normalized_side = str(side or "").lower()
    normalized_symbol = str(symbol or "").upper()
    source_thesis_id = str(source_thesis_id or "")
    closed = _closed_bars(bars_5m)
    base = {
        "qualified": False,
        "reason": "",
        "failed_gates": [],
        "symbol": normalized_symbol,
        "side": normalized_side,
        "source": "SMC_RR_PASS_PAIRED_SHADOW",
        "source_thesis_id": source_thesis_id,
        "market_regime": str((market_regime or {}).get("label") or "UNKNOWN").upper(),
        "context_15m": dict(context_15m or {}),
        "source_snapshot": dict(source_snapshot or {}),
    }
    if normalized_side not in {"buy", "sell"} or not source_thesis_id:
        base.update(reason="INVALID_EXTERNAL_THESIS_IDENTITY", failed_gates=["identity"])
        return base
    if len(closed) < 20:
        base.update(reason="INSUFFICIENT_CLOSED_5M_DATA", failed_gates=["data"])
        return base

    latest = closed.iloc[-1]
    close = _finite(latest.get("close"))
    atr = _finite(_atr(closed).iloc[-1])
    invalidation = _finite(invalidation_level)
    structurally_valid = (
        invalidation < close if normalized_side == "buy" else invalidation > close
    )
    if close <= 0 or atr <= 0:
        base.update(reason="INVALID_CLOSED_5M_REFERENCE", failed_gates=["data"])
        return base
    if invalidation <= 0 or not structurally_valid:
        base.update(
            reason="THESIS_ALREADY_STRUCTURALLY_INVALID_AT_REGISTRATION",
            failed_gates=["structural_invalidation"],
        )
        return base

    impulse_bar_id = _bar_id(latest)
    setup_id = _stable_id(
        "mc_smc_setup",
        {
            "source_thesis_id": source_thesis_id,
            "symbol": normalized_symbol,
            "side": normalized_side,
            "reference_bar": impulse_bar_id,
        },
    )
    base.update(
        qualified=True,
        reason="EXTERNAL_SMC_THESIS_REGISTERED_FOR_PAIRED_SHADOW",
        setup_id=setup_id,
        impulse_bar_id=impulse_bar_id,
        impulse_open_time=str(latest.get("timestamp", latest.get("time", latest.name))),
        impulse_price=close,
        breakout_level=close,
        invalidation_level=invalidation,
        atr_5m=atr,
        evidence={
            "reference_is_closed_5m": True,
            "reference_close": close,
            "reference_atr_5m": atr,
            "original_invalidation": invalidation,
        },
    )
    return base


def detect_momentum_impulse(
    *,
    symbol: str,
    candidate: dict,
    bars_5m: pd.DataFrame,
    market_regime: dict | None,
    context_15m: dict | None,
    breakout_lookback: int = 20,
    min_volume_ratio: float = 1.30,
    min_range_atr: float = 0.80,
    max_range_atr: float = 2.50,
    max_abs_24h_change_pct: float = 30.0,
    min_quote_volume: float = 10_000_000.0,
) -> dict:
    """Detect a closed-candle displacement; never enter from a live tick."""
    side = candidate_side(candidate)
    closed = _closed_bars(bars_5m)
    base = {
        "qualified": False,
        "symbol": str(symbol or "").upper(),
        "side": side,
        "reason": "",
        "failed_gates": [],
        "evidence": {},
    }
    if side is None:
        base.update(reason="NO_DIRECTIONAL_MOMENTUM_TAG", failed_gates=["direction"])
        return base
    if len(closed) < max(55, breakout_lookback + 16):
        base.update(reason="INSUFFICIENT_CLOSED_5M_DATA", failed_gates=["data"])
        return base

    tags = {str(tag).upper() for tag in candidate.get("tags", [])}
    latest = closed.iloc[-1]
    prior = closed.iloc[-(breakout_lookback + 1):-1]
    atr = _finite(_atr(closed).iloc[-1])
    open_price = _finite(latest.get("open"))
    high = _finite(latest.get("high"))
    low = _finite(latest.get("low"))
    close = _finite(latest.get("close"))
    volume = _finite(latest.get("volume"))
    candle_range = max(0.0, high - low)
    body_ratio = abs(close - open_price) / candle_range if candle_range > 0 else 0.0
    close_location = (close - low) / candle_range if candle_range > 0 else 0.5
    median_volume = _finite(prior["volume"].tail(20).median())
    volume_ratio = volume / median_volume if median_volume > 0 else 0.0
    prior_high = _finite(prior["high"].max())
    prior_low = _finite(prior["low"].min())
    range_atr = candle_range / atr if atr > 0 else 0.0
    ema_20 = _finite(closed["close"].ewm(span=20, adjust=False).mean().iloc[-1])
    ema_50 = _finite(closed["close"].ewm(span=50, adjust=False).mean().iloc[-1])

    regime = str((market_regime or {}).get("label") or "UNKNOWN").upper()
    regime_known = bool((market_regime or {}).get("known", regime != "UNKNOWN"))
    regime_opposes = (side == "buy" and regime in RISK_OFF) or (
        side == "sell" and regime in RISK_ON
    )
    context = context_15m or {}
    local_aligned = bool(
        (side == "buy" and context.get("up_structure"))
        or (side == "sell" and context.get("down_structure"))
    )
    directional = close > open_price if side == "buy" else close < open_price
    breakout_level = prior_high if side == "buy" else prior_low
    breakout = close > prior_high if side == "buy" else close < prior_low
    close_quality = close_location >= 0.70 if side == "buy" else close_location <= 0.30
    ema_aligned = ema_20 > ema_50 if side == "buy" else ema_20 < ema_50
    quote_volume = _finite(candidate.get("quote_volume_24h"))
    change_24h = abs(_finite(candidate.get("change_24h_pct")))

    gates = {
        "not_new_contract": "NEW_CONTRACT" not in tags,
        "liquidity": quote_volume >= float(min_quote_volume),
        "not_extreme_24h": change_24h <= float(max_abs_24h_change_pct),
        "regime_known": regime_known,
        "regime_policy": not regime_opposes,
        "local_15m_structure": local_aligned,
        "ema_5m_alignment": ema_aligned,
        "directional_body": directional and body_ratio >= 0.55,
        "close_location": close_quality,
        "breakout": breakout,
        "volume_expansion": volume_ratio >= float(min_volume_ratio),
        "displacement_range": (
            atr > 0
            and float(min_range_atr) <= range_atr <= float(max_range_atr)
        ),
    }
    failed = [name for name, passed in gates.items() if not passed]
    bar_id = _bar_id(latest)
    setup_id = _stable_id(
        "mc_setup",
        {"symbol": symbol, "side": side, "impulse_bar_id": bar_id},
    )
    invalidation = (
        min(low, breakout_level - atr * 0.15)
        if side == "buy"
        else max(high, breakout_level + atr * 0.15)
    )
    base.update(
        qualified=not failed,
        reason="IMPULSE_CONFIRMED" if not failed else "FAILED:" + ",".join(failed),
        failed_gates=failed,
        setup_id=setup_id,
        impulse_bar_id=bar_id,
        impulse_open_time=str(latest.get("timestamp", latest.get("time", latest.name))),
        impulse_price=close,
        impulse_high=high,
        impulse_low=low,
        breakout_level=breakout_level,
        invalidation_level=invalidation,
        atr_5m=atr,
        evidence={
            "gates": gates,
            "market_regime": regime,
            "change_24h_pct": _finite(candidate.get("change_24h_pct")),
            "change_1m_pct": _finite(candidate.get("change_1m_pct")),
            "change_5m_pct": _finite(candidate.get("change_5m_pct")),
            "quote_volume_24h": quote_volume,
            "body_ratio": body_ratio,
            "close_location": close_location,
            "volume_ratio": volume_ratio,
            "range_atr": range_atr,
            "ema_20": ema_20,
            "ema_50": ema_50,
            "context_15m": context,
        },
    )
    return base


def evaluate_continuation_confirmation(
    *,
    setup: dict,
    bars_5m: pd.DataFrame,
    max_wait_bars: int = 3,
    retest_buffer_atr: float = 0.35,
    max_extension_atr: float = 1.25,
    min_volume_ratio: float = 0.75,
) -> dict:
    """Wait for a later closed 5m hold candle; the impulse itself cannot enter."""
    closed = _closed_bars(bars_5m)
    result = {
        "status": "WAIT",
        "passed": False,
        "reason": "",
        "failed_gates": [],
        "evidence": {},
    }
    if closed.empty:
        result.update(status="DATA_UNAVAILABLE", reason="INSUFFICIENT_CLOSED_5M_DATA")
        return result

    impulse_id = str(setup.get("impulse_bar_id") or "")
    ids = [_bar_id(row) for _, row in closed.iterrows()]
    try:
        impulse_position = ids.index(impulse_id)
    except ValueError:
        result.update(status="DATA_UNAVAILABLE", reason="IMPULSE_BAR_NOT_IN_WINDOW")
        return result

    later = closed.iloc[impulse_position + 1:]
    if later.empty:
        result.update(reason="WAITING_FOR_NEXT_CLOSED_5M_BAR")
        return result
    if len(later) > int(max_wait_bars):
        result.update(status="EXPIRED", reason="CONFIRMATION_WINDOW_EXPIRED")
        return result

    row = later.iloc[-1]
    side = str(setup.get("side") or "").lower()
    atr = _finite(setup.get("atr_5m"))
    breakout = _finite(setup.get("breakout_level"))
    invalidation = _finite(setup.get("invalidation_level"))
    open_price = _finite(row.get("open"))
    high = _finite(row.get("high"))
    low = _finite(row.get("low"))
    close = _finite(row.get("close"))
    candle_range = max(0.0, high - low)
    close_location = (close - low) / candle_range if candle_range > 0 else 0.5
    history = closed.iloc[: impulse_position + 1]
    median_volume = _finite(history["volume"].tail(20).median())
    volume_ratio = _finite(row.get("volume")) / median_volume if median_volume > 0 else 0.0
    directional = close > open_price if side == "buy" else close < open_price
    holds_breakout = (
        low >= breakout - atr * retest_buffer_atr and close > breakout
        if side == "buy"
        else high <= breakout + atr * retest_buffer_atr and close < breakout
    )
    not_invalidated = close > invalidation if side == "buy" else close < invalidation
    extension_atr = abs(close - breakout) / atr if atr > 0 else float("inf")
    close_quality = close_location >= 0.60 if side == "buy" else close_location <= 0.40
    gates = {
        "not_invalidated": not_invalidated,
        "holds_breakout": holds_breakout,
        "directional_close": directional,
        "close_location": close_quality,
        "participation": volume_ratio >= float(min_volume_ratio),
        "not_extended": extension_atr <= float(max_extension_atr),
    }
    failed = [name for name, passed in gates.items() if not passed]
    hard_invalidated = not not_invalidated
    status = "HARD_INVALIDATED" if hard_invalidated else "PASS" if not failed else "WAIT"
    result.update(
        status=status,
        passed=not failed,
        reason="CONTINUATION_CONFIRMED" if not failed else "FAILED:" + ",".join(failed),
        failed_gates=failed,
        confirmation_bar_id=_bar_id(row),
        confirmation_open_time=str(row.get("timestamp", row.get("time", row.name))),
        entry_price=close,
        confirmation_high=high,
        confirmation_low=low,
        evidence={
            "gates": gates,
            "bars_after_impulse": len(later),
            "volume_ratio": volume_ratio,
            "extension_atr": extension_atr,
            "close_location": close_location,
        },
    )
    return result


def _collect_targets(side: str, entry: float, levels: dict | None) -> list[dict]:
    levels = levels or {}
    targets: list[dict] = []
    if side == "buy":
        targets.extend(
            {"type": "bearish_fvg", "price": _finite(item.get("low"))}
            for item in levels.get("bearish_fvgs", [])
        )
        targets.extend(
            {"type": "bearish_ob_supply", "price": _finite(item.get("low"))}
            for item in levels.get("bearish_obs", [])
        )
        targets.extend(
            {"type": "swing_high_liquidity", "price": _finite(item.get("price"))}
            for item in levels.get("swing_highs", [])
        )
        return [item for item in targets if item["price"] > entry]
    targets.extend(
        {"type": "bullish_fvg", "price": _finite(item.get("high"))}
        for item in levels.get("bullish_fvgs", [])
    )
    targets.extend(
        {"type": "bullish_ob_demand", "price": _finite(item.get("high"))}
        for item in levels.get("bullish_obs", [])
    )
    targets.extend(
        {"type": "swing_low_liquidity", "price": _finite(item.get("price"))}
        for item in levels.get("swing_lows", [])
    )
    return [item for item in targets if 0 < item["price"] < entry]


def build_fresh_continuation_plan(
    *,
    setup: dict,
    confirmation: dict,
    market_levels: dict | None,
    min_net_rr: float = 1.50,
    round_trip_fee_pct: float = 0.0008,
    slippage_pct: float = 0.0004,
    min_stop_pct: float = 0.004,
    max_stop_pct: float = 0.03,
    min_stop_atr: float = 0.50,
    max_stop_atr: float = 2.50,
) -> dict:
    """Build entry, structural stop and first real target from current data."""
    side = str(setup.get("side") or "").lower()
    entry = _finite(confirmation.get("entry_price"))
    atr = _finite(setup.get("atr_5m"))
    setup_invalidation = _finite(setup.get("invalidation_level"))
    confirmation_low = _finite(confirmation.get("confirmation_low"))
    confirmation_high = _finite(confirmation.get("confirmation_high"))
    if side == "buy":
        invalidation = min(setup_invalidation, confirmation_low - atr * 0.10)
        risk = entry - invalidation
    else:
        invalidation = max(setup_invalidation, confirmation_high + atr * 0.10)
        risk = invalidation - entry

    base = {
        "valid": False,
        "execution_enabled": False,
        "plan_type": "FRESH_CONTINUATION_SHADOW",
        "setup_id": setup.get("setup_id"),
        "side": side,
        "entry": entry,
        "sl": invalidation,
        "reason": "",
    }
    if side not in ("buy", "sell") or entry <= 0 or risk <= 0 or atr <= 0:
        base["reason"] = "INVALID_ENTRY_OR_STRUCTURAL_STOP"
        return base

    stop_pct = risk / entry
    stop_atr = risk / atr
    base.update(risk=risk, stop_pct=stop_pct, stop_atr=stop_atr)
    if not (float(min_stop_pct) <= stop_pct <= float(max_stop_pct)):
        base["reason"] = "STOP_DISTANCE_OUTSIDE_PERCENT_POLICY"
        return base
    if not (float(min_stop_atr) <= stop_atr <= float(max_stop_atr)):
        base["reason"] = "STOP_DISTANCE_OUTSIDE_ATR_POLICY"
        return base

    targets = sorted(
        _collect_targets(side, entry, market_levels),
        key=lambda item: abs(item["price"] - entry),
    )
    if not targets:
        base["reason"] = "NO_REAL_OPPOSING_LIQUIDITY_TARGET"
        return base
    target = targets[0]
    raw_target = target["price"]
    tp = raw_target * (1 - 0.0015) if side == "buy" else raw_target * (1 + 0.0015)
    gross_reward = tp - entry if side == "buy" else entry - tp
    estimated_cost = entry * (float(round_trip_fee_pct) + float(slippage_pct))
    net_reward = gross_reward - estimated_cost
    gross_rr = gross_reward / risk if risk > 0 else 0.0
    net_rr = net_reward / risk if risk > 0 else 0.0
    plan_id = _stable_id(
        "mc_plan",
        {
            "setup_id": setup.get("setup_id"),
            "confirmation_bar_id": confirmation.get("confirmation_bar_id"),
            "entry": f"{entry:.10g}",
            "sl": f"{invalidation:.10g}",
            "tp": f"{tp:.10g}",
        },
    )
    base.update(
        plan_id=plan_id,
        tp=tp,
        raw_target=raw_target,
        target_type=target["type"],
        gross_reward=gross_reward,
        estimated_cost=estimated_cost,
        gross_rr=gross_rr,
        net_rr=net_rr,
        min_net_rr=float(min_net_rr),
        evaluated_targets=targets[:20],
        confirmation_bar_id=confirmation.get("confirmation_bar_id"),
        confirmation_open_time=confirmation.get("confirmation_open_time"),
    )
    if gross_reward <= 0:
        base["reason"] = "TARGET_NOT_PROFITABLE_AFTER_FRONT_RUN"
        return base
    if net_rr < float(min_net_rr):
        base["reason"] = "FIRST_TARGET_NET_RR_TOO_LOW"
        return base
    base.update(valid=True, reason="QUALIFIED_SHADOW_PLAN")
    return base


def evaluate_shadow_plan_outcome(
    *,
    plan: dict,
    bars_5m: pd.DataFrame,
    max_horizon_minutes: int = 240,
    now: datetime | None = None,
) -> dict:
    """Resolve TP/SL ordering from later closed candles without look-ahead."""
    closed = _closed_bars(bars_5m)
    result = {
        "terminal": False,
        "status": "OPEN",
        "mfe_r": 0.0,
        "mae_r": 0.0,
        "final_r": None,
    }
    if closed.empty:
        return result
    side = str(plan.get("side") or "").lower()
    entry = _finite(plan.get("entry"))
    sl = _finite(plan.get("sl"))
    tp = _finite(plan.get("tp"))
    risk = abs(entry - sl)
    confirmed_at = _utc_timestamp(plan.get("confirmation_open_time"))
    if side not in ("buy", "sell") or risk <= 0 or confirmed_at is None:
        return {**result, "terminal": True, "status": "INVALID_PLAN"}

    timestamps = closed.get("timestamp", pd.Series(closed.index, index=closed.index)).apply(
        _utc_timestamp
    )
    forward = closed.loc[timestamps > confirmed_at]
    if forward.empty:
        return result

    if side == "buy":
        mfe = max(0.0, _finite(forward["high"].max()) - entry) / risk
        mae = max(0.0, entry - _finite(forward["low"].min())) / risk
    else:
        mfe = max(0.0, entry - _finite(forward["low"].min())) / risk
        mae = max(0.0, _finite(forward["high"].max()) - entry) / risk
    result.update(mfe_r=mfe, mae_r=mae)

    for _, row in forward.iterrows():
        high = _finite(row.get("high"))
        low = _finite(row.get("low"))
        target_hit = high >= tp if side == "buy" else low <= tp
        stop_hit = low <= sl if side == "buy" else high >= sl
        if target_hit and stop_hit:
            return {
                **result,
                "terminal": True,
                "status": "AMBIGUOUS_SAME_CANDLE",
                "final_r": None,
                "terminal_bar_id": _bar_id(row),
            }
        if target_hit:
            return {
                **result,
                "terminal": True,
                "status": "TARGET_FIRST",
                "final_r": _finite(plan.get("net_rr"), _finite(plan.get("gross_rr"))),
                "terminal_bar_id": _bar_id(row),
            }
        if stop_hit:
            return {
                **result,
                "terminal": True,
                "status": "STOP_FIRST",
                "final_r": -1.0,
                "terminal_bar_id": _bar_id(row),
            }

    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    if (now_ts - confirmed_at).total_seconds() >= int(max_horizon_minutes) * 60:
        close = _finite(forward.iloc[-1].get("close"))
        final_r = (close - entry) / risk if side == "buy" else (entry - close) / risk
        return {
            **result,
            "terminal": True,
            "status": "HORIZON_EXIT",
            "final_r": final_r,
            "terminal_bar_id": _bar_id(forward.iloc[-1]),
        }
    return result
