"""Immutable domain objects used by the prospective Shadow Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Candle:
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    taker_buy_quote_volume: float = 0.0

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None or self.close_time.tzinfo is None:
            raise ValueError("candle timestamps must be timezone-aware")
        if self.close_time <= self.open_time:
            raise ValueError("candle close must follow open")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC geometry")

    @property
    def candle_id(self) -> str:
        return f"{self.timeframe}:{int(self.open_time.timestamp() * 1000)}"


@dataclass(frozen=True)
class Quote:
    observed_at: datetime
    best_bid: float
    best_ask: float
    mark_price: float
    index_price: float
    funding_rate: float
    next_funding_time: datetime | None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("quote timestamp must be timezone-aware")
        if not 0 < self.best_bid <= self.best_ask:
            raise ValueError("invalid executable quote")

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float:
        mid = (self.best_bid + self.best_ask) / 2.0
        return self.spread / mid * 10_000.0


@dataclass(frozen=True)
class ExecutionQuote:
    """One causally observed, ordered public best-bid/best-ask event."""

    symbol: str
    event_time: datetime
    received_at: datetime
    best_bid: float
    best_ask: float
    update_id: int
    source: str = "BINANCE_DEMO_PUBLIC_BOOK_TICKER"

    def __post_init__(self) -> None:
        if self.event_time.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("execution quote timestamps must be timezone-aware")
        if not 0 < self.best_bid <= self.best_ask:
            raise ValueError("invalid executable quote")

    @property
    def event_id(self) -> str:
        return f"{self.symbol}:{self.update_id}:{int(self.event_time.timestamp() * 1000)}"

    @property
    def spread_bps(self) -> float:
        mid = (self.best_bid + self.best_ask) / 2.0
        return (self.best_ask - self.best_bid) / mid * 10_000.0


@dataclass(frozen=True)
class CandleOpenEvent:
    """Prospectively captured first 1m kline update for a new candle."""

    symbol: str
    open_time: datetime
    received_at: datetime
    open_price: float
    source: str = "BINANCE_DEMO_PUBLIC_KLINE_1M"

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("candle-open timestamps must be timezone-aware")
        if self.open_price <= 0:
            raise ValueError("candle open must be positive")

    @property
    def event_id(self) -> str:
        return f"{self.symbol}:1m:{int(self.open_time.timestamp() * 1000)}"


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    symbol: str
    side: str
    emitted_at: datetime
    event: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.emitted_at.tzinfo is None:
            raise ValueError("candidate timestamp must be timezone-aware")
        if self.side != "LONG":
            raise ValueError("R3 Active Altcoin population is LONG-only")
        object.__setattr__(self, "event", MappingProxyType(dict(self.event)))


@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_id: str
    symbol: str
    as_of: datetime
    candidate: Candidate
    quote: Quote
    candles: Mapping[str, tuple[Candle, ...]]
    source: str = "BINANCE_DEMO_PUBLIC_USDM"

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("snapshot timestamp must be timezone-aware")
        frozen = {}
        for timeframe, rows in dict(self.candles).items():
            ordered = tuple(sorted(rows, key=lambda row: row.close_time))
            if any(row.close_time > self.as_of for row in ordered):
                raise ValueError("snapshot contains an open/future candle")
            frozen[str(timeframe)] = ordered
        object.__setattr__(self, "candles", MappingProxyType(frozen))

    def closed(self, timeframe: str) -> tuple[Candle, ...]:
        return self.candles.get(timeframe, ())

    @property
    def closed_candle_ids(self) -> dict[str, str | None]:
        return {
            timeframe: rows[-1].candle_id if rows else None
            for timeframe, rows in self.candles.items()
        }


@dataclass(frozen=True)
class Geometry:
    executable_quote_time: datetime
    executable_entry: float
    entry_quote_side: str
    entry_price_source: str
    old_reference_entry: float | None
    stop: float
    stop_source: str
    stop_level_id: str
    target: float
    target_source: str
    target_level_id: str
    planned_rr: float
    actual_rr_at_entry: float
    geometry_change_reason: str

    @property
    def risk_distance(self) -> float:
        return self.executable_entry - self.stop

    def as_dict(self) -> dict[str, Any]:
        return {
            "executable_quote_time": utc_iso(self.executable_quote_time),
            "executable_entry": self.executable_entry,
            "entry_quote_side": self.entry_quote_side,
            "entry_price_source": self.entry_price_source,
            "old_reference_entry": self.old_reference_entry,
            "stop": self.stop,
            "stop_source": self.stop_source,
            "stop_level_id": self.stop_level_id,
            "target": self.target,
            "target_source": self.target_source,
            "target_level_id": self.target_level_id,
            "planned_RR": self.planned_rr,
            "actual_RR_at_entry": self.actual_rr_at_entry,
            "geometry_change_reason": self.geometry_change_reason,
        }


@dataclass
class PolicyDecision:
    policy_id: str
    candidate_id: str
    thesis_id: str
    stage: str
    status: str
    state_before: str | None
    state_after: str | None
    reason_codes: list[str]
    entry_ready_at: datetime | None = None
    geometry: Geometry | None = None
    level_map: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
