"""Small, closed-candle benchmark regime model for execution policy.

The model intentionally uses only BTC and ETH USD-M futures.  It is not an
alpha generator and it does not guess a top or bottom.  Its only job is to
stop the bot from confusing an altcoin's relative weakness with a safe short
while the two market benchmarks are structurally risk-on (and vice versa).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


REGIME_UNKNOWN = "UNKNOWN"
STRONG_RISK_ON = "STRONG_RISK_ON"
MILD_RISK_ON = "MILD_RISK_ON"
RANGE = "RANGE"
MILD_RISK_OFF = "MILD_RISK_OFF"
STRONG_RISK_OFF = "STRONG_RISK_OFF"
HIGH_VOLATILITY_EVENT = "HIGH_VOLATILITY_EVENT"


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _benchmark_snapshot(frame: pd.DataFrame | None) -> dict | None:
    if frame is None or len(frame) < 210:
        return None
    required = {"high", "low", "close"}
    if not required.issubset(frame.columns):
        return None

    bars = frame.copy()
    for column in required:
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    # REST/WS include the live 1h candle at [-1].  Regime uses closed bars only.
    closed = bars.iloc[:-1].dropna(subset=["high", "low", "close"])
    if len(closed) < 209:
        return None

    close = closed["close"]
    ema_50 = close.ewm(span=50, adjust=False).mean()
    ema_200 = close.ewm(span=200, adjust=False).mean()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            closed["high"] - closed["low"],
            (closed["high"] - previous_close).abs(),
            (closed["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(14).mean()

    last_close = _finite(close.iloc[-1])
    last_ema_50 = _finite(ema_50.iloc[-1])
    last_ema_200 = _finite(ema_200.iloc[-1])
    last_atr = _finite(atr.iloc[-1])
    median_atr = _finite(atr.tail(120).median())
    ema_50_slope = _finite(ema_50.iloc[-1] - ema_50.iloc[-7])
    return_24h = (
        (last_close / _finite(close.iloc[-25]) - 1.0)
        if len(close) >= 25 and _finite(close.iloc[-25]) > 0
        else 0.0
    )
    return {
        "close": last_close,
        "ema_50": last_ema_50,
        "ema_200": last_ema_200,
        "ema_50_slope_atr": ema_50_slope / last_atr if last_atr > 0 else 0.0,
        "return_24h": return_24h,
        "atr_pct": last_atr / last_close if last_close > 0 else 0.0,
        "volatility_ratio": last_atr / median_atr if median_atr > 0 else 1.0,
        "up_structure": bool(
            last_close > last_ema_50 > last_ema_200 > 0 and ema_50_slope > 0
        ),
        "down_structure": bool(
            0 < last_close < last_ema_50 < last_ema_200 and ema_50_slope < 0
        ),
    }


def classify_market_regime(
    btc_bars: pd.DataFrame | None,
    eth_bars: pd.DataFrame | None,
) -> dict:
    btc = _benchmark_snapshot(btc_bars)
    eth = _benchmark_snapshot(eth_bars)
    if btc is None or eth is None:
        return {
            "label": REGIME_UNKNOWN,
            "known": False,
            "reason": "INSUFFICIENT_CLOSED_BENCHMARK_DATA",
            "btc": btc,
            "eth": eth,
        }

    both_up = btc["up_structure"] and eth["up_structure"]
    both_down = btc["down_structure"] and eth["down_structure"]
    positive_breadth = sum((btc["return_24h"] > 0, eth["return_24h"] > 0))
    negative_breadth = sum((btc["return_24h"] < 0, eth["return_24h"] < 0))
    high_volatility = max(
        btc["volatility_ratio"], eth["volatility_ratio"]
    ) >= 1.75

    if both_up and positive_breadth == 2:
        label = STRONG_RISK_ON
        reason = "BTC_ETH_UP_STRUCTURE_AND_POSITIVE_24H"
    elif both_down and negative_breadth == 2:
        label = STRONG_RISK_OFF
        reason = "BTC_ETH_DOWN_STRUCTURE_AND_NEGATIVE_24H"
    elif (
        btc["up_structure"]
        and positive_breadth >= 1
        and not eth["down_structure"]
    ):
        label = MILD_RISK_ON
        reason = "BTC_UP_STRUCTURE_WITH_NON_BEARISH_BREADTH"
    elif (
        btc["down_structure"]
        and negative_breadth >= 1
        and not eth["up_structure"]
    ):
        label = MILD_RISK_OFF
        reason = "BTC_DOWN_STRUCTURE_WITH_NON_BULLISH_BREADTH"
    elif high_volatility:
        label = HIGH_VOLATILITY_EVENT
        reason = "ATR_REGIME_SPIKE_WITHOUT_DIRECTIONAL_CONSENSUS"
    else:
        label = RANGE
        reason = "NO_BTC_ETH_DIRECTIONAL_CONSENSUS"

    return {
        "label": label,
        "known": True,
        "reason": reason,
        "high_volatility": high_volatility,
        "btc": btc,
        "eth": eth,
    }


def detect_market_regime(client) -> dict:
    """Fetch one closed-candle benchmark snapshot for an entire scan round."""
    try:
        btc = client.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=260)
        eth = client.fetch_ohlcv("ETH/USDT", timeframe="1h", limit=260)
        return classify_market_regime(btc, eth)
    except Exception as exc:
        return {
            "label": REGIME_UNKNOWN,
            "known": False,
            "reason": f"BENCHMARK_FETCH_FAILED:{type(exc).__name__}",
            "btc": None,
            "eth": None,
        }
