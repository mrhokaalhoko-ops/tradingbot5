"""Closed OHLCV input contracts for pure, point-in-time structure research."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .causality import require_utc


def _decimal(value: Decimal, name: str, *, positive: bool = True) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    invalid = value <= 0 if positive else value < 0
    if invalid:
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")


@dataclass(frozen=True, slots=True)
class ClosedBar:
    bar_id: str
    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    final: bool

    def __post_init__(self) -> None:
        for name in ("bar_id", "symbol", "timeframe"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} required")
        for name in ("open_time", "close_time", "available_at"):
            require_utc(getattr(self, name), name)
        if not self.open_time < self.close_time <= self.available_at:
            raise ValueError("bar time ordering invalid")
        if self.final is not True:
            raise ValueError("only final closed bars are accepted")
        for name in ("open", "high", "low", "close"):
            _decimal(getattr(self, name), name)
        _decimal(self.volume, "volume", positive=False)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high below OHLC maximum")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low above OHLC minimum")


def validate_bar_series(bars: tuple[ClosedBar, ...], *, as_of: datetime) -> tuple[ClosedBar, ...]:
    """Reject incomplete, mixed, noncausal, or non-prefix-safe input; never filter it."""
    require_utc(as_of, "as_of")
    if not isinstance(bars, tuple) or any(type(bar) is not ClosedBar for bar in bars):
        raise TypeError("bars must be a tuple of ClosedBar")
    ids: set[str] = set()
    for index, bar in enumerate(bars):
        if bar.bar_id in ids:
            raise ValueError("duplicate bar_id")
        ids.add(bar.bar_id)
        if bar.available_at > as_of:
            raise ValueError("future unavailable bar")
        if index:
            previous = bars[index - 1]
            if (bar.symbol != previous.symbol or bar.timeframe != previous.timeframe):
                raise ValueError("mixed symbol or timeframe")
            if (bar.open_time < previous.close_time or
                    bar.close_time <= previous.close_time or
                    bar.available_at <= previous.available_at):
                raise ValueError("bars must be chronological with strictly increasing availability")
    return bars
