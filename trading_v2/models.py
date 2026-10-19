"""Typed domain objects for the replacement decision engine.

These objects deliberately contain no CCXT client, notifier, state manager or
order method. This keeps research causal and prevents backtest-only shortcuts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class EntryKind(str, Enum):
    MARKET_NEXT_OPEN = "MARKET_NEXT_OPEN"
    LIMIT = "LIMIT"


class ReplayStatus(str, Enum):
    TP_FIRST = "TP_FIRST"
    SL_FIRST = "SL_FIRST"
    OPEN_AT_HORIZON = "OPEN_AT_HORIZON"
    EXPIRED_UNFILLED = "EXPIRED_UNFILLED"
    TARGET_BEFORE_ENTRY = "TARGET_BEFORE_ENTRY"
    INVALIDATED_BEFORE_ENTRY = "INVALIDATED_BEFORE_ENTRY"
    AMBIGUOUS_SAME_BAR = "AMBIGUOUS_SAME_BAR"
    AMBIGUOUS_ENTRY_BAR = "AMBIGUOUS_ENTRY_BAR"
    INVALID_PLAN = "INVALID_PLAN"
    NO_EXECUTION_BARS = "NO_EXECUTION_BARS"
    INCOMPLETE_DATA = "INCOMPLETE_DATA"


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _positive_finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


@dataclass(frozen=True)
class Bar:
    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        _require_aware(self.open_time, "open_time")
        _require_aware(self.close_time, "close_time")
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        if not str(self.symbol).strip() or not str(self.timeframe).strip():
            raise ValueError("symbol and timeframe are required")
        open_price = _positive_finite(self.open, "open")
        high = _positive_finite(self.high, "high")
        low = _positive_finite(self.low, "low")
        close = _positive_finite(self.close, "close")
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ValueError("invalid OHLC geometry")
        if not math.isfinite(float(self.volume)) or float(self.volume) < 0:
            raise ValueError("volume must be finite and non-negative")


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    as_of: datetime
    bars: Mapping[str, tuple[Bar, ...]]
    regime: str = "UNKNOWN"
    features: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        frozen_bars: dict[str, tuple[Bar, ...]] = {}
        for timeframe, raw_bars in dict(self.bars).items():
            ordered = tuple(sorted(tuple(raw_bars), key=lambda bar: bar.close_time))
            if any(bar.symbol != self.symbol for bar in ordered):
                raise ValueError("snapshot cannot mix symbols")
            if any(bar.close_time > self.as_of for bar in ordered):
                raise ValueError("snapshot contains a future or still-open bar")
            frozen_bars[str(timeframe)] = ordered
        object.__setattr__(self, "bars", MappingProxyType(frozen_bars))
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))

    def closed_bars(self, timeframe: str) -> tuple[Bar, ...]:
        return self.bars.get(timeframe, ())


@dataclass(frozen=True)
class TradePlan:
    plan_id: str
    strategy_id: str
    setup_id: str
    symbol: str
    side: Side
    created_at: datetime
    valid_until: datetime
    exit_by: datetime
    entry_kind: EntryKind
    entry: float
    stop: float
    target: float
    thesis: str
    score: float = 0.0
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.valid_until, "valid_until")
        _require_aware(self.exit_by, "exit_by")
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be after created_at")
        if self.exit_by <= self.created_at:
            raise ValueError("exit_by must be after created_at")
        if self.exit_by < self.valid_until:
            raise ValueError("exit_by cannot precede valid_until")
        for name in ("plan_id", "strategy_id", "setup_id", "symbol", "thesis"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        entry = _positive_finite(self.entry, "entry")
        stop = _positive_finite(self.stop, "stop")
        target = _positive_finite(self.target, "target")
        if self.side is Side.BUY and not (stop < entry < target):
            raise ValueError("BUY plan requires stop < entry < target")
        if self.side is Side.SELL and not (target < entry < stop):
            raise ValueError("SELL plan requires target < entry < stop")
        if not math.isfinite(float(self.score)):
            raise ValueError("score must be finite")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    @property
    def risk_distance(self) -> float:
        return abs(float(self.entry) - float(self.stop))

    @property
    def planned_rr(self) -> float:
        return abs(float(self.target) - float(self.entry)) / self.risk_distance


@dataclass(frozen=True)
class ReplayResult:
    plan_id: str
    status: ReplayStatus
    filled_at: datetime | None = None
    exited_at: datetime | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    gross_r: float | None = None
    net_r: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    bars_observed: int = 0
    reason: str = ""
