"""Deterministic offline fixtures used by tests and the mandatory dry-run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import math

from .models import Candidate, Candle, MarketSnapshot, Quote


TIMEFRAME_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "1h": 60,
    "4h": 240, "1d": 1_440, "1w": 10_080,
}


def _candles(timeframe: str, count: int, as_of: datetime, *, center: float = 100.0) -> tuple[Candle, ...]:
    minutes = TIMEFRAME_MINUTES[timeframe]
    delta = timedelta(minutes=minutes)
    first_close = as_of - delta * (count - 1)
    rows = []
    for index in range(count):
        close_time = first_close + delta * index
        # Upward drift plus a repeatable wave creates causally confirmed pivots.
        close = center - 8.0 + 8.0 * index / max(count - 1, 1) + 0.65 * math.sin(index * 0.42)
        open_price = close - 0.12 * math.cos(index * 0.31)
        high = max(open_price, close) + 0.42
        low = min(open_price, close) - 0.42
        rows.append(Candle(
            timeframe=timeframe,
            open_time=close_time - delta + timedelta(milliseconds=1),
            close_time=close_time,
            open=open_price, high=high, low=low, close=close,
            volume=1_000.0 + index * 7.0,
            quote_volume=(1_000.0 + index * 7.0) * close,
            taker_buy_quote_volume=(1_000.0 + index * 7.0) * close * 0.48,
        ))
    return tuple(rows)


def synthetic_snapshot(
    *,
    as_of: datetime | None = None,
    candidate: Candidate | None = None,
    bid: float = 100.00,
    ask: float = 100.02,
    snapshot_suffix: str = "initial",
) -> MarketSnapshot:
    as_of = (as_of or datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)).astimezone(timezone.utc)
    candidate = candidate or Candidate(
        candidate_id="aas_event_fixture_r3",
        symbol="FIXTUREUSDT",
        side="LONG",
        emitted_at=as_of,
        event={
            "event_id": "aas_event_fixture_r3",
            "symbol": "FIXTUREUSDT",
            "side": "LONG",
            "open_time": (as_of - timedelta(minutes=5)).isoformat(),
            "close_time": as_of.isoformat(),
            "open": 98.0,
            "high": 100.25,
            "low": 99.60,
            "close": 100.0,
            "atr": 0.80,
            "event_score": 10.5,
            "qualified": True,
            "gates": {"all": True},
        },
    )
    candles = {
        "1w": _candles("1w", 40, as_of),
        "1d": _candles("1d", 90, as_of),
        "4h": _candles("4h", 120, as_of, center=99.0),
        "1h": _candles("1h", 240, as_of, center=103.0),
        "15m": _candles("15m", 240, as_of),
        "5m": _candles("5m", 120, as_of),
        "1m": _candles("1m", 180, as_of),
    }
    digest = hashlib.sha256(
        f"{candidate.candidate_id}|{as_of.isoformat()}|{bid}|{ask}|{snapshot_suffix}".encode()
    ).hexdigest()[:24]
    return MarketSnapshot(
        snapshot_id=f"snapshot_fixture_{digest}",
        symbol=candidate.symbol,
        as_of=as_of,
        candidate=candidate,
        quote=Quote(
            observed_at=as_of,
            best_bid=bid,
            best_ask=ask,
            mark_price=(bid + ask) / 2.0,
            index_price=(bid + ask) / 2.0,
            funding_rate=0.0001,
            next_funding_time=as_of + timedelta(hours=4),
        ),
        candles=candles,
        source="OFFLINE_DETERMINISTIC_FIXTURE",
    )
