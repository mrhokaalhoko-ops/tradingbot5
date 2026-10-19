"""Causal directional event-study metrics for strategy discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Iterable, Mapping

from .models import Side


@dataclass(frozen=True)
class DirectionalEvent:
    symbol: str
    side: Side
    timestamp: datetime
    entry_price: float
    exit_price: float
    directional_gross_bps: float
    directional_net_bps: float
    opposite_net_bps: float
    mfe_pct: float
    mae_pct: float
    features: Mapping[str, object] = field(default_factory=dict)


def calendar_split_events(
    events: Iterable[DirectionalEvent],
    *,
    start: datetime,
    end: datetime,
    train_ratio: float = 0.60,
    validation_ratio: float = 0.20,
) -> dict[str, tuple[DirectionalEvent, ...]]:
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("start/end must be timezone-aware and ordered")
    if not 0 < train_ratio < 1 or not 0 <= validation_ratio < 1:
        raise ValueError("invalid split ratios")
    if train_ratio + validation_ratio >= 1:
        raise ValueError("train + validation must leave a test segment")
    rows = tuple(sorted(events, key=lambda event: (event.timestamp, event.symbol)))
    if any(event.timestamp < start or event.timestamp > end for event in rows):
        raise ValueError("event timestamp outside evaluation window")
    duration = end - start
    train_end = start + duration * train_ratio
    validation_end = start + duration * (train_ratio + validation_ratio)
    return {
        "TRAIN": tuple(event for event in rows if event.timestamp < train_end),
        "VALIDATION": tuple(
            event for event in rows if train_end <= event.timestamp < validation_end
        ),
        "TEST": tuple(event for event in rows if event.timestamp >= validation_end),
    }


def summarize_events(events: Iterable[DirectionalEvent]) -> dict:
    rows = tuple(events)
    original = [float(event.directional_net_bps) for event in rows]
    opposite = [float(event.opposite_net_bps) for event in rows]
    return {
        "events": len(rows),
        "original_positive_rate": (
            sum(value > 0 for value in original) / len(original) if original else None
        ),
        "original_mean_net_bps": sum(original) / len(original) if original else None,
        "original_median_net_bps": median(original) if original else None,
        "opposite_positive_rate": (
            sum(value > 0 for value in opposite) / len(opposite) if opposite else None
        ),
        "opposite_mean_net_bps": sum(opposite) / len(opposite) if opposite else None,
        "mean_mfe_pct": (
            sum(float(event.mfe_pct) for event in rows) / len(rows) if rows else None
        ),
        "mean_mae_pct": (
            sum(float(event.mae_pct) for event in rows) / len(rows) if rows else None
        ),
        "buy_events": sum(event.side is Side.BUY for event in rows),
        "sell_events": sum(event.side is Side.SELL for event in rows),
    }


def summarize_feature_buckets(
    events: Iterable[DirectionalEvent],
    feature: str,
) -> dict[str, dict]:
    grouped: dict[str, list[DirectionalEvent]] = {}
    for event in events:
        value = str(event.features.get(feature, "UNKNOWN"))
        grouped.setdefault(value, []).append(event)
    return {
        value: summarize_events(rows)
        for value, rows in sorted(grouped.items())
    }
