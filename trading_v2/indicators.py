"""Small deterministic indicators for the V2 research strategies."""

from __future__ import annotations

from statistics import mean
from typing import Sequence

from .models import Bar


def ema(values: Sequence[float], period: int) -> float:
    if period <= 0 or len(values) < period:
        raise ValueError("not enough values for EMA")
    alpha = 2.0 / (period + 1.0)
    value = mean(float(item) for item in values[:period])
    for item in values[period:]:
        value = alpha * float(item) + (1.0 - alpha) * value
    return value


def atr(bars: Sequence[Bar], period: int = 14) -> float:
    if period <= 0 or len(bars) < period + 1:
        raise ValueError("not enough bars for ATR")
    ranges = []
    for previous, current in zip(bars[-period - 1:-1], bars[-period:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return mean(ranges)


def close_location(bar: Bar) -> float:
    span = float(bar.high) - float(bar.low)
    return 0.5 if span <= 0 else (float(bar.close) - float(bar.low)) / span


def average_volume(bars: Sequence[Bar], period: int = 20) -> float:
    if period <= 0 or len(bars) < period:
        raise ValueError("not enough bars for average volume")
    return mean(float(bar.volume) for bar in bars[-period:])
