"""Look-ahead-safe higher-timeframe market context primitives."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "known": False,
        "timeframe": "4h",
        "trend": "UNKNOWN",
        "strength": 0,
        "structure": "UNKNOWN",
        "atr_percent": None,
        "distance_from_ema_atr": None,
        "extended": None,
        "nearest_support_zone": None,
        "nearest_resistance_zone": None,
        "last_closed_at": None,
        "closed_candle_only": True,
        "reason": reason,
    }


def _confirmed_pivots(frame: pd.DataFrame, radius: int = 2) -> tuple[list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    values = frame.tail(80).reset_index(drop=True)
    for index in range(radius, len(values) - radius):
        window = values.iloc[index - radius:index + radius + 1]
        high = _number(values.iloc[index].get("high"))
        low = _number(values.iloc[index].get("low"))
        if high is not None and high >= float(window["high"].max()):
            highs.append(high)
        if low is not None and low <= float(window["low"].min()):
            lows.append(low)
    return highs, lows


def _zone(price: float | None, width: float) -> dict[str, float] | None:
    if price is None or price <= 0:
        return None
    half = max(width, price * 0.0005)
    return {"low": round(price - half, 10), "high": round(price + half, 10)}


def analyze_market_context_4h(bars: pd.DataFrame) -> dict[str, Any]:
    """Build 4H context strictly from candles closed at decision time.

    Binance/CCXT typically returns the forming candle as the final row, so it
    is excluded unconditionally.  Confirmed pivots also require two already
    closed candles to their right, preventing a future-data pivot.
    """

    if bars is None or not isinstance(bars, pd.DataFrame) or len(bars) < 211:
        return _unknown("INSUFFICIENT_4H_HISTORY")
    required = {"open", "high", "low", "close"}
    if not required.issubset(bars.columns):
        return _unknown("MISSING_4H_OHLC_COLUMNS")

    closed = bars.iloc[:-1].copy()
    for column in required:
        closed[column] = pd.to_numeric(closed[column], errors="coerce")
    closed = closed.dropna(subset=list(required))
    if len(closed) < 210:
        return _unknown("INSUFFICIENT_VALID_4H_HISTORY")

    close_series = closed["close"]
    ema50 = close_series.ewm(span=50, adjust=False).mean()
    ema200 = close_series.ewm(span=200, adjust=False).mean()
    prior_close = close_series.shift(1)
    true_range = pd.concat([
        closed["high"] - closed["low"],
        (closed["high"] - prior_close).abs(),
        (closed["low"] - prior_close).abs(),
    ], axis=1).max(axis=1)
    atr = true_range.rolling(14).mean()

    last_close = float(close_series.iloc[-1])
    last_ema50 = float(ema50.iloc[-1])
    last_ema200 = float(ema200.iloc[-1])
    last_atr = _number(atr.iloc[-1])
    if last_close <= 0 or last_ema50 <= 0 or last_ema200 <= 0 or not last_atr or last_atr <= 0:
        return _unknown("INVALID_4H_INDICATORS")

    highs, lows = _confirmed_pivots(closed)
    structure = "MIXED"
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            structure = "HH_HL"
        elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            structure = "LH_LL"

    vote = 0.0
    vote += 2.0 if last_ema50 > last_ema200 else -2.0
    vote += 1.0 if last_close > last_ema50 else -1.0
    slope = last_ema50 - float(ema50.iloc[-6])
    vote += 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
    vote += 1.0 if structure == "HH_HL" else (-1.0 if structure == "LH_LL" else 0.0)

    if vote >= 2.0:
        trend = "BULLISH"
    elif vote <= -2.0:
        trend = "BEARISH"
    else:
        trend = "RANGE"
    strength = int(round(min(100.0, abs(vote) / 5.0 * 100.0)))
    distance_ema_atr = abs(last_close - last_ema50) / last_atr
    extended = distance_ema_atr >= 2.5

    support = max((value for value in lows if value < last_close), default=None)
    resistance = min((value for value in highs if value > last_close), default=None)
    zone_width = last_atr * 0.25

    last_row = closed.iloc[-1]
    raw_time = last_row.get("time", last_row.get("timestamp"))
    if hasattr(raw_time, "isoformat"):
        last_closed_at = raw_time.isoformat()
    elif raw_time is None or (isinstance(raw_time, float) and math.isnan(raw_time)):
        last_closed_at = None
    else:
        last_closed_at = str(raw_time)

    return {
        "known": True,
        "timeframe": "4h",
        "trend": trend,
        "strength": strength,
        "structure": structure,
        "close": round(last_close, 10),
        "ema50": round(last_ema50, 10),
        "ema200": round(last_ema200, 10),
        "ema50_slope": round(slope, 10),
        "atr": round(last_atr, 10),
        "atr_percent": round(last_atr / last_close, 8),
        "distance_from_ema_atr": round(distance_ema_atr, 4),
        "extended": extended,
        "nearest_support_zone": _zone(support, zone_width),
        "nearest_resistance_zone": _zone(resistance, zone_width),
        "last_closed_at": last_closed_at,
        "closed_candle_only": True,
        "reason": (
            f"EMA50 {'فوق' if last_ema50 > last_ema200 else 'تحت'} EMA200؛ "
            f"البنية {structure}؛ البعد {distance_ema_atr:.2f} ATR"
        ),
    }
