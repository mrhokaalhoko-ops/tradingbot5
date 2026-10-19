"""Closed-candle 1H setup classification for deterministic diagnostics."""

from __future__ import annotations

import math
from typing import Any, Mapping

import pandas as pd


def _finite(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "known": False,
        "timeframe": "1h",
        "support": None,
        "resistance": None,
        "atr": None,
        "rvol_20": None,
        "long_state": "UNKNOWN",
        "short_state": "UNKNOWN",
        "last_closed_at": None,
        "closed_candle_only": True,
        "reason": reason,
    }


def analyze_setup_context_1h(bars: pd.DataFrame) -> dict[str, Any]:
    """Classify support/resistance interaction using the last closed 1H bar."""

    if bars is None or not isinstance(bars, pd.DataFrame) or len(bars) < 32:
        return _unknown("INSUFFICIENT_1H_HISTORY")
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(bars.columns):
        return _unknown("MISSING_1H_OHLCV_COLUMNS")
    closed = bars.iloc[:-1].copy()
    for column in required:
        closed[column] = pd.to_numeric(closed[column], errors="coerce")
    closed = closed.dropna(subset=list(required))
    if len(closed) < 31:
        return _unknown("INSUFFICIENT_VALID_1H_HISTORY")

    last = closed.iloc[-1]
    history = closed.iloc[-21:-1]
    resistance = _finite(history["high"].max())
    support = _finite(history["low"].min())
    prior_close = closed["close"].shift(1)
    true_range = pd.concat([
        closed["high"] - closed["low"],
        (closed["high"] - prior_close).abs(),
        (closed["low"] - prior_close).abs(),
    ], axis=1).max(axis=1)
    atr = _finite(true_range.rolling(14).mean().iloc[-1])
    volume_mean = _finite(closed["volume"].shift(1).rolling(20).mean().iloc[-1])
    volume = _finite(last.get("volume"))
    rvol = (volume / volume_mean) if volume and volume_mean and volume_mean > 0 else None
    open_price = float(last["open"])
    high = float(last["high"])
    low = float(last["low"])
    close = float(last["close"])
    candle_range = max(high - low, 1e-12)
    bullish_close_strength = (close - low) / candle_range
    bearish_close_strength = (high - close) / candle_range

    if not resistance or not support or not atr or atr <= 0:
        return _unknown("INVALID_1H_LEVEL_OR_ATR")
    up_tolerance = max(0.15 * atr, resistance * 0.001)
    down_tolerance = max(0.15 * atr, support * 0.001)

    if close > resistance + up_tolerance:
        long_state = (
            "CONFIRMED_BREAKOUT"
            if bullish_close_strength >= 0.60 and (rvol is None or rvol >= 1.20)
            else "CLOSE_BREAK"
        )
    elif high > resistance + up_tolerance:
        long_state = "WICK_BREAK"
    elif high >= resistance - up_tolerance:
        long_state = "TOUCH"
    else:
        long_state = "INSIDE_RANGE"

    if close < support - down_tolerance:
        short_state = (
            "CONFIRMED_BREAKDOWN"
            if bearish_close_strength >= 0.60 and (rvol is None or rvol >= 1.20)
            else "CLOSE_BREAK"
        )
    elif low < support - down_tolerance:
        short_state = "WICK_BREAK"
    elif low <= support + down_tolerance:
        short_state = "TOUCH"
    else:
        short_state = "INSIDE_RANGE"

    raw_time = last.get("time", last.get("timestamp"))
    last_closed_at = raw_time.isoformat() if hasattr(raw_time, "isoformat") else (
        None if raw_time is None else str(raw_time)
    )
    return {
        "known": True,
        "timeframe": "1h",
        "support": round(support, 10),
        "resistance": round(resistance, 10),
        "atr": round(atr, 10),
        "rvol_20": None if rvol is None else round(rvol, 4),
        "close": round(close, 10),
        "bullish_close_strength": round(bullish_close_strength, 4),
        "bearish_close_strength": round(bearish_close_strength, 4),
        "long_state": long_state,
        "short_state": short_state,
        "long_next_trigger": f"إغلاق 1H قوي فوق {resistance:.10g} ثم ثبات المستوى",
        "short_next_trigger": f"إغلاق 1H قوي تحت {support:.10g} ثم ثبات المستوى",
        "last_closed_at": last_closed_at,
        "closed_candle_only": True,
        "reason": (
            f"LONG={long_state}; SHORT={short_state}; RVOL="
            f"{rvol:.2f}" if rvol is not None else f"LONG={long_state}; SHORT={short_state}; RVOL=N/A"
        ),
    }


def select_setup_side(context: Mapping[str, Any], side: str) -> dict[str, Any]:
    """Return the side-specific view without manufacturing missing evidence."""

    result = dict(context or {})
    normalized = str(side or "").lower()
    if normalized == "buy":
        result["selected_state"] = result.get("long_state", "UNKNOWN")
        result["next_trigger"] = result.get("long_next_trigger")
    elif normalized == "sell":
        result["selected_state"] = result.get("short_state", "UNKNOWN")
        result["next_trigger"] = result.get("short_next_trigger")
    else:
        result["selected_state"] = "UNKNOWN"
        result["next_trigger"] = None
    result["side"] = normalized.upper() if normalized else None
    return result
