"""Causal global-regime model built only from closed BTC and ETH bars."""

from __future__ import annotations

from .indicators import atr, ema
from .models import Bar


REGIMES = {
    "STRONG_RISK_ON",
    "MILD_RISK_ON",
    "RANGE",
    "MILD_RISK_OFF",
    "STRONG_RISK_OFF",
    "HIGH_VOLATILITY_EVENT",
    "UNKNOWN",
}


def classify_global_regime(
    btc_1h: tuple[Bar, ...],
    eth_1h: tuple[Bar, ...],
) -> str:
    """Return a deliberately small, interpretable market regime.

    This is a research feature, not a claim of profitability.  It prevents a
    relatively weak altcoin from being called an absolute short while both
    market anchors remain structurally bullish.
    """
    if len(btc_1h) < 51 or len(eth_1h) < 51:
        return "UNKNOWN"

    def state(bars: tuple[Bar, ...]) -> tuple[int, float, float]:
        closes = [bar.close for bar in bars]
        fast = ema(closes, 20)
        slow = ema(closes, 50)
        six_hour_return = closes[-1] / closes[-7] - 1.0
        direction = 1 if closes[-1] > fast > slow else -1 if closes[-1] < fast < slow else 0
        atr_pct = atr(bars, 14) / closes[-1]
        return direction, six_hour_return, atr_pct

    btc_direction, btc_return, btc_atr = state(btc_1h)
    eth_direction, eth_return, eth_atr = state(eth_1h)
    if max(btc_atr, eth_atr) >= 0.035:
        return "HIGH_VOLATILITY_EVENT"
    if btc_direction == eth_direction == 1:
        return "STRONG_RISK_ON" if min(btc_return, eth_return) >= 0.01 else "MILD_RISK_ON"
    if btc_direction == eth_direction == -1:
        return "STRONG_RISK_OFF" if max(btc_return, eth_return) <= -0.01 else "MILD_RISK_OFF"
    return "RANGE"
