"""Pure state transitions for the evidence-backed active-altcoin shadow path.

The module contains no exchange client and no order API. It consumes public
one-minute candles, creates a causal closed-candle event, and follows a
counterfactual pullback/reclaim plan for prospective validation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd

from trading_v2.active_altcoin_scalper import ActiveAltcoinConfig, MAJOR_SYMBOLS


MICRO_FLOW_PRICE_AGREEMENT_MAX = 0.24334596546967147
MIN_EVENT_SCORE = 8.0


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> pd.Timestamp:
    result = pd.Timestamp(value)
    return result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")


def _stable_id(prefix: str, payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:20]}"


def _closed_1m(frame: pd.DataFrame, now: Any) -> pd.DataFrame:
    required = {
        "open_time", "close_time", "open", "high", "low", "close", "volume",
        "quote_volume", "taker_buy_quote_volume",
    }
    if frame is None or not required.issubset(frame.columns):
        return pd.DataFrame(columns=sorted(required))
    rows = frame.copy()
    rows["open_time"] = pd.to_datetime(rows["open_time"], utc=True)
    rows["close_time"] = pd.to_datetime(rows["close_time"], utc=True)
    for column in required - {"open_time", "close_time"}:
        rows[column] = pd.to_numeric(rows[column], errors="coerce")
    rows = rows.dropna(subset=list(required)).sort_values("open_time")
    # Duplicate timestamps cannot substitute for a missing minute. Discard
    # every ambiguous copy so downstream continuity checks detect the gap.
    rows = rows.drop_duplicates("open_time", keep=False)
    numeric = list(required - {"open_time", "close_time"})
    rows = rows.loc[np.isfinite(rows[numeric]).all(axis=1)]
    expected_close = rows["open_time"] + pd.Timedelta(minutes=1) - pd.Timedelta(milliseconds=1)
    rows = rows.loc[
        rows["open_time"].eq(rows["open_time"].dt.floor("min"))
        & rows["close_time"].eq(expected_close)
    ]
    return rows.loc[rows["close_time"].le(_timestamp(now))].reset_index(drop=True)


def aggregate_closed_5m(frame: pd.DataFrame, now: Any) -> pd.DataFrame:
    """Aggregate only completed 1m rows into epoch-aligned completed 5m bars."""
    closed = _closed_1m(frame, now)
    if closed.empty:
        return pd.DataFrame()
    source = closed.set_index("open_time")
    bars = source.resample("5min", origin="epoch", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        quote_volume=("quote_volume", "sum"),
        source_bars=("close", "count"),
    ).dropna(subset=["open", "high", "low", "close"])
    bars = bars.loc[bars["source_bars"].eq(5)].reset_index()
    bars["close_time"] = bars["open_time"] + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1)
    return bars.loc[bars["close_time"].le(_timestamp(now))].reset_index(drop=True)


def event_micro_features(event: dict, frame: pd.DataFrame, now: Any) -> dict:
    """Compute event-close flow features from its five constituent 1m bars."""
    closed = _closed_1m(frame, now)
    start = _timestamp(event["open_time"])
    end = _timestamp(event["close_time"])
    recent = closed.loc[closed["open_time"].ge(start) & closed["close_time"].le(end)]
    if len(recent) != 5:
        return {"valid": False, "reason": "INCOMPLETE_EVENT_1M_BARS"}
    direction = 1.0 if event["side"] == "LONG" else -1.0
    opens = recent["open"].astype(float)
    closes = recent["close"].astype(float)
    quote = recent["quote_volume"].astype(float).clip(lower=0.0)
    taker_buy = recent["taker_buy_quote_volume"].astype(float).clip(lower=0.0)
    imbalance = pd.Series(
        np.where(quote.gt(0), 2.0 * taker_buy / quote - 1.0, 0.0),
        index=recent.index,
    ).clip(-1.0, 1.0)
    signed_body_return = direction * (closes / opens - 1.0) * 100.0
    directional_imbalance = direction * imbalance
    agreement = float((directional_imbalance * signed_body_return).mean())
    return {
        "valid": True,
        "flow_price_agreement": agreement,
        "gate_threshold": MICRO_FLOW_PRICE_AGREEMENT_MAX,
        "gate_passed": agreement <= MICRO_FLOW_PRICE_AGREEMENT_MAX,
        "directional_taker_imbalance_mean": float(directional_imbalance.mean()),
        "directional_bar_fraction": float((signed_body_return > 0).mean()),
    }


def detect_active_event(
    symbol: str,
    frame: pd.DataFrame,
    now: Any,
    config: ActiveAltcoinConfig = ActiveAltcoinConfig(),
) -> dict:
    """Describe the latest completed 5m bar and all frozen event gates."""
    symbol = str(symbol or "").upper().replace("/", "").split(":", 1)[0]
    bars = aggregate_closed_5m(frame, now)
    if len(bars) < 25:
        return {"base_qualified": False, "qualified": False, "reason": "INSUFFICIENT_HISTORY"}
    if not bars.tail(25)["open_time"].diff().dropna().eq(pd.Timedelta(minutes=5)).all():
        return {"base_qualified": False, "qualified": False, "reason": "GAPPED_EVENT_HISTORY"}
    latest = bars.iloc[-1]
    close = bars["close"].astype(float)
    return_5m = float(close.pct_change(1).iloc[-1] * 100.0)
    return_20m = float(close.pct_change(4).iloc[-1] * 100.0)
    previous_close = close.shift(1)
    true_range = pd.concat([
        bars["high"].astype(float) - bars["low"].astype(float),
        (bars["high"].astype(float) - previous_close).abs(),
        (bars["low"].astype(float) - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr = float(true_range.rolling(14, min_periods=14).mean().iloc[-1])
    volume_base = float(bars["quote_volume"].astype(float).iloc[-21:-1].mean())
    volume_ratio = float(latest["quote_volume"]) / volume_base if volume_base > 0 else 0.0
    candle_range = float(latest["high"] - latest["low"])
    body_fraction = abs(float(latest["close"] - latest["open"])) / candle_range if candle_range > 0 else 0.0
    close_location = (float(latest["close"] - latest["low"]) / candle_range) if candle_range > 0 else 0.5
    same_direction = np.sign(return_5m) == np.sign(return_20m) and return_5m != 0
    directional_close = close_location >= config.directional_close_boundary if return_5m > 0 else close_location <= 1.0 - config.directional_close_boundary
    gates = {
        "altcoin": symbol not in MAJOR_SYMBOLS,
        "return_5m": abs(return_5m) >= config.min_abs_5m_pct,
        "return_20m": abs(return_20m) >= config.min_abs_20m_pct,
        "same_direction": bool(same_direction),
        "volume_expansion": volume_ratio >= config.min_volume_ratio,
        "quote_volume_5m": float(latest["quote_volume"]) >= config.min_quote_volume_5m,
        "body_fraction": body_fraction >= config.min_body_fraction,
        "directional_close": bool(directional_close),
        "atr_valid": atr > 0,
    }
    side = "LONG" if return_5m > 0 else "SHORT"
    event_score = abs(return_5m) + 0.5 * abs(return_20m) + float(np.log1p(max(volume_ratio, 0.0)))
    event = {
        "event_id": _stable_id("aas_event", {
            "symbol": symbol, "close_time": str(latest["close_time"]), "side": side,
        }),
        "symbol": symbol,
        "side": side,
        "open_time": _timestamp(latest["open_time"]).isoformat(),
        "close_time": _timestamp(latest["close_time"]).isoformat(),
        "open": float(latest["open"]),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
        "close": float(latest["close"]),
        "atr": atr,
        "return_5m_pct": return_5m,
        "return_20m_pct": return_20m,
        "volume_ratio": volume_ratio,
        "quote_volume_5m": float(latest["quote_volume"]),
        "body_fraction": body_fraction,
        "close_location": close_location,
        "event_score": event_score,
        "gates": gates,
        "base_qualified": all(gates.values()),
    }
    event["micro"] = event_micro_features(event, frame, now)
    event["score_passed"] = event_score >= MIN_EVENT_SCORE
    event["qualified"] = bool(
        event["base_qualified"]
        and event["score_passed"]
        and event["micro"].get("gate_passed")
    )
    failed = [name for name, passed in gates.items() if not passed]
    if failed:
        event["reason"] = "FAILED:" + ",".join(failed)
    elif not event["score_passed"]:
        event["reason"] = "EVENT_SCORE_BELOW_RESEARCH_FLOOR"
    elif not event["micro"].get("valid"):
        event["reason"] = event["micro"].get("reason")
    elif not event["micro"].get("gate_passed"):
        event["reason"] = "CROWDED_FLOW_PRICE_AGREEMENT"
    else:
        event["reason"] = "ACTIVE_EVENT_QUALIFIED_FOR_SHADOW"
    return event


def new_setup(event: dict) -> dict:
    return {
        "setup_id": _stable_id("aas_setup", {"event_id": event["event_id"]}),
        "event": dict(event),
        "symbol": event["symbol"],
        "side": event["side"],
        "status": "WAITING_PULLBACK",
        "touched": False,
        "extreme": None,
        "last_checked_close_time": event["close_time"],
    }


def advance_setup(
    setup: dict,
    frame: pd.DataFrame,
    now: Any,
    config: ActiveAltcoinConfig = ActiveAltcoinConfig(),
) -> dict:
    """Advance a setup through 1m pullback and reclaim states."""
    result = dict(setup)
    # Once confirmed, subsequent candles belong to the trade outcome, not to
    # a second entry decision. Preserve the trigger across polls and restart.
    if result.get("status") in {"RECLAIM_CONFIRMED", "HARD_INVALIDATED", "UNCONFIRMED"}:
        return result
    event = result["event"]
    closed = _closed_1m(frame, now)
    signal_time = _timestamp(event["close_time"])
    last_checked = _timestamp(result.get("last_checked_close_time", signal_time))
    deadline = signal_time + pd.Timedelta(minutes=config.pullback_wait_bars * 5)
    unseen = closed.loc[
        closed["close_time"].gt(last_checked) & closed["close_time"].le(deadline)
    ]
    side = event["side"]
    atr = float(event["atr"])
    event_close = float(event["close"])
    touched = bool(result.get("touched"))
    extreme = result.get("extreme")
    expected_open = last_checked.floor("min") + pd.Timedelta(minutes=1)
    for _, bar in unseen.iterrows():
        if _timestamp(bar["open_time"]) != expected_open:
            result["status"] = "DATA_GAP"
            return result
        expected_open += pd.Timedelta(minutes=1)
        high, low = float(bar["high"]), float(bar["low"])
        candle_range = high - low
        result["last_checked_close_time"] = _timestamp(bar["close_time"]).isoformat()
        if side == "LONG":
            if low <= event_close - config.hard_invalidation_atr * atr:
                result.update(status="HARD_INVALIDATED", invalidated_at=result["last_checked_close_time"])
                return result
            extreme = low if extreme is None else min(float(extreme), low)
            touched = touched or low <= event_close - config.pullback_atr * atr
            reclaimed = bool(
                touched
                and float(bar["close"]) > float(bar["open"])
                and float(bar["close"]) >= event_close - config.reclaim_distance_atr * atr
                and candle_range > 0
                and (float(bar["close"]) - low) / candle_range >= config.directional_close_boundary
            )
        else:
            if high >= event_close + config.hard_invalidation_atr * atr:
                result.update(status="HARD_INVALIDATED", invalidated_at=result["last_checked_close_time"])
                return result
            extreme = high if extreme is None else max(float(extreme), high)
            touched = touched or high >= event_close + config.pullback_atr * atr
            reclaimed = bool(
                touched
                and float(bar["close"]) < float(bar["open"])
                and float(bar["close"]) <= event_close + config.reclaim_distance_atr * atr
                and candle_range > 0
                and (high - float(bar["close"])) / candle_range >= config.directional_close_boundary
            )
        result.update(touched=touched, extreme=extreme)
        if reclaimed:
            result.update(
                status="RECLAIM_CONFIRMED",
                confirmation_close_time=result["last_checked_close_time"],
            )
            return result
    expected_close = expected_open + pd.Timedelta(minutes=1) - pd.Timedelta(milliseconds=1)
    if expected_close <= min(_timestamp(now), deadline):
        result["status"] = "DATA_GAP"
    elif _timestamp(now) > deadline:
        result["status"] = "UNCONFIRMED"
    elif touched:
        result["status"] = "SWEEP_PENDING"
    return result


def build_shadow_plan(
    setup: dict,
    frame: pd.DataFrame,
    config: ActiveAltcoinConfig = ActiveAltcoinConfig(),
    *, now: Any = None,
) -> dict:
    """Use the exact next 1m open after a confirmed reclaim as the shadow fill."""
    if frame is None or "open_time" not in frame.columns:
        return {"valid": False, "reason": "WAITING_NEXT_1M_OPEN"}
    confirmation_time = _timestamp(setup["confirmation_close_time"])
    entry_time = confirmation_time.floor("min") + pd.Timedelta(minutes=1)
    rows = frame.copy()
    rows["open_time"] = pd.to_datetime(rows["open_time"], utc=True)
    if now is not None:
        rows = rows.loc[rows["open_time"].le(_timestamp(now))]
    next_rows = rows.loc[rows["open_time"].ge(entry_time)].sort_values("open_time")
    if next_rows.empty:
        return {"valid": False, "reason": "WAITING_NEXT_1M_OPEN"}
    exact_rows = next_rows.loc[next_rows["open_time"].eq(entry_time)]
    if len(exact_rows) != 1:
        return {"valid": False, "reason": "MISSING_NEXT_1M_OPEN"}
    entry_bar = exact_rows.iloc[0]
    event = setup["event"]
    side = setup["side"]
    atr = float(event["atr"])
    entry = float(entry_bar["open"])
    extreme = float(setup["extreme"])
    stop = extreme - config.stop_buffer_atr * atr if side == "LONG" else extreme + config.stop_buffer_atr * atr
    risk = entry - stop if side == "LONG" else stop - entry
    if (
        side not in {"LONG", "SHORT"}
        or not all(np.isfinite(value) and value > 0 for value in (entry, stop, atr, risk))
        or not config.min_risk_atr * atr <= risk <= config.max_risk_atr * atr
    ):
        return {"valid": False, "reason": "INVALID_RISK"}
    target = entry + config.target_r * risk if side == "LONG" else entry - config.target_r * risk
    plan_id = _stable_id("aas_plan", {
        "setup_id": setup["setup_id"], "filled_at": str(entry_bar["open_time"]),
        "entry": entry, "stop": stop, "target": target,
    })
    return {
        "valid": True,
        "execution_enabled": False,
        "plan_id": plan_id,
        "setup_id": setup["setup_id"],
        "event_id": event["event_id"],
        "symbol": setup["symbol"],
        "side": side,
        "filled_at": _timestamp(entry_bar["open_time"]).isoformat(),
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk": risk,
        "target_r": config.target_r,
        "round_trip_cost_bps": config.round_trip_cost_bps,
        "status": "OPEN",
    }


def evaluate_shadow_plan(
    plan: dict,
    frame: pd.DataFrame,
    now: Any,
    config: ActiveAltcoinConfig = ActiveAltcoinConfig(),
) -> dict:
    """Evaluate the shadow plan using closed 1m OHLC, conservatively."""
    closed = _closed_1m(frame, now)
    filled_at = _timestamp(plan["filled_at"])
    deadline = filled_at + pd.Timedelta(minutes=config.holding_bars * 5)
    path = closed.loc[
        closed["open_time"].ge(filled_at) & closed["open_time"].lt(deadline)
    ].sort_values("open_time")
    def data_gap(expected_open):
        return {
            "terminal": False, "status": "DATA_GAP",
            "expected_open_time": expected_open.isoformat(),
        }

    if path.empty:
        if _timestamp(now) >= filled_at + pd.Timedelta(minutes=1) - pd.Timedelta(milliseconds=1):
            return data_gap(filled_at)
        return {"terminal": False, "status": "OPEN"}
    side = plan["side"]
    entry, stop, target, risk = (float(plan[key]) for key in ("entry", "stop", "target", "risk"))
    direction = 1.0 if side == "LONG" else -1.0
    status = "OPEN"
    exit_price = None
    exited_at = None
    mfe_r = mae_r = 0.0
    expected_open = filled_at
    for _, bar in path.iterrows():
        if _timestamp(bar["open_time"]) != expected_open:
            return data_gap(expected_open)
        expected_open += pd.Timedelta(minutes=1)
        high, low = float(bar["high"]), float(bar["low"])
        if side == "LONG":
            mfe_r = max(mfe_r, (high - entry) / risk)
            mae_r = max(mae_r, (entry - low) / risk)
            stop_hit, target_hit = low <= stop, high >= target
        else:
            mfe_r = max(mfe_r, (entry - low) / risk)
            mae_r = max(mae_r, (high - entry) / risk)
            stop_hit, target_hit = high >= stop, low <= target
        if stop_hit or target_hit:
            status = "AMBIGUOUS_AS_LOSS" if stop_hit and target_hit else "SL_FIRST" if stop_hit else "TP_FIRST"
            exit_price = stop if stop_hit else target
            exited_at = _timestamp(bar["close_time"]).isoformat()
            break
    if status == "OPEN":
        expected_close = expected_open + pd.Timedelta(minutes=1) - pd.Timedelta(milliseconds=1)
        if expected_open < deadline and expected_close <= _timestamp(now):
            return data_gap(expected_open)
    if status == "OPEN" and _timestamp(now) >= deadline:
        status = "TIME_EXIT"
        exit_price = float(path.iloc[-1]["close"])
        exited_at = _timestamp(path.iloc[-1]["close_time"]).isoformat()
    if status == "OPEN":
        return {"terminal": False, "status": status, "mfe_r": mfe_r, "mae_r": mae_r}
    gross_r = -1.0 if status == "AMBIGUOUS_AS_LOSS" else direction * (exit_price - entry) / risk
    cost_r = (config.round_trip_cost_bps / 10_000.0 * entry) / risk
    return {
        "terminal": True,
        "status": status,
        "exited_at": exited_at,
        "exit_price": exit_price,
        "gross_r": gross_r,
        "net_r": gross_r - cost_r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
    }
