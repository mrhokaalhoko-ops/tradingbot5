"""Leakage-safe measurements for newly listed USD-M Futures contracts.

This module is deliberately an event study, not a trading strategy.  It only
uses the first 30 minutes after the observed launch to classify an event and
keeps every forward return strictly after that observation window.  No result
from this module authorizes live orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Iterable, Sequence

from .binance_archive import KlineRow


BAR_MS = 5 * 60 * 1000
HORIZONS_HOURS = (1, 4, 12, 24, 72)


@dataclass(frozen=True)
class LaunchEvent:
    symbol: str
    launch_time: datetime
    anchor_time: datetime | None
    anchor_delay_minutes: float | None
    status: str
    bars_observed: int
    first_30m_return_pct: float | None
    first_30m_range_pct: float | None
    first_30m_close_location: float | None
    first_30m_taker_buy_ratio: float | None
    candidate_side: str
    returns_pct: dict[str, float | None]
    candidate_returns_pct: dict[str, float | None]
    mfe_pct_24h: float | None
    mae_pct_24h: float | None
    data_gap_count_72h: int


def _as_utc(value_ms: int) -> datetime:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc)


def _bar_map(rows: Iterable[KlineRow]) -> dict[int, KlineRow]:
    """Deduplicate timestamps and reject impossible duplicate OHLC rows."""

    result: dict[int, KlineRow] = {}
    for row in sorted(rows, key=lambda item: item.open_time_ms):
        previous = result.get(row.open_time_ms)
        if previous is not None and previous != row:
            raise ValueError("conflicting duplicate 5m timestamps")
        result[row.open_time_ms] = row
    return result


def find_anchor_index(
    rows: Sequence[KlineRow], launch_time_ms: int, *, max_delay_ms: int = 60 * 60 * 1000
) -> int | None:
    """Return the first 5m bar at/after the 1h launch observation.

    A delay of up to one hour is accepted because a newly listed contract can
    have no trades in the first several 5m buckets while the 1h archive still
    records its first active bucket.  A larger delay is treated as invalid
    rather than silently using a distant future bar.
    """

    for index, row in enumerate(rows):
        delay = row.open_time_ms - int(launch_time_ms)
        if delay < 0:
            continue
        if delay <= max_delay_ms:
            return index
        return None
    return None


def _contiguous(
    rows: Sequence[KlineRow], start: int, count: int, *, interval_ms: int = BAR_MS
) -> bool:
    if start < 0 or start + count > len(rows):
        return False
    return all(
        rows[index].open_time_ms == rows[start].open_time_ms + (index - start) * interval_ms
        for index in range(start, start + count)
    )


def _horizon_close(
    rows: Sequence[KlineRow], start: int, hours: int, *, interval_ms: int = BAR_MS
) -> KlineRow | None:
    bars = hours * 60 * 60 * 1000 // interval_ms
    index = start + bars - 1
    if index >= len(rows) or not _contiguous(rows, start, bars, interval_ms=interval_ms):
        return None
    return rows[index]


def _pct(numerator: float, denominator: float) -> float | None:
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0:
        return None
    return 100.0 * (numerator / denominator - 1.0)


def _candidate_side(
    first_return_pct: float | None,
    close_location: float | None,
    taker_buy_ratio: float | None,
    *,
    return_threshold_pct: float = 5.0,
    long_close_location: float = 0.60,
    short_close_location: float = 0.40,
    long_taker_ratio: float = 0.55,
    short_taker_ratio: float = 0.45,
) -> str:
    """Classify a conspicuous first-30m expansion for descriptive analysis.

    Thresholds are frozen and reported as an event-study gate.  They are not
    claimed to be optimal and must not be wired directly into execution.
    """

    if first_return_pct is None or close_location is None or taker_buy_ratio is None:
        return "NONE"
    if (
        first_return_pct >= return_threshold_pct
        and close_location >= long_close_location
        and taker_buy_ratio >= long_taker_ratio
    ):
        return "LONG"
    if (
        first_return_pct <= -return_threshold_pct
        and close_location <= short_close_location
        and taker_buy_ratio <= short_taker_ratio
    ):
        return "SHORT"
    return "NONE"


def measure_launch_event(
    symbol: str,
    launch_time: datetime,
    rows: Iterable[KlineRow],
    *,
    classification_return_threshold_pct: float = 5.0,
    bar_minutes: int = 5,
) -> LaunchEvent:
    """Measure one launch using only bars available in its two archives."""

    if launch_time.tzinfo is None:
        raise ValueError("launch_time must be timezone-aware")
    if bar_minutes not in {1, 5}:
        raise ValueError("bar_minutes must be 1 or 5")
    interval_ms = bar_minutes * 60 * 1000
    observation_bars = 30 // bar_minutes
    ordered = sorted(_bar_map(rows).values(), key=lambda row: row.open_time_ms)
    launch_ms = int(launch_time.timestamp() * 1000)
    anchor_index = find_anchor_index(ordered, launch_ms)
    if anchor_index is None:
        return LaunchEvent(
            symbol=symbol,
            launch_time=launch_time,
            anchor_time=None,
            anchor_delay_minutes=None,
            status="INVALID_ANCHOR",
            bars_observed=0,
            first_30m_return_pct=None,
            first_30m_range_pct=None,
            first_30m_close_location=None,
            first_30m_taker_buy_ratio=None,
            candidate_side="NONE",
            returns_pct={f"{hours}h": None for hours in HORIZONS_HOURS},
            candidate_returns_pct={f"{hours}h": None for hours in HORIZONS_HOURS},
            mfe_pct_24h=None,
            mae_pct_24h=None,
            data_gap_count_72h=0,
        )

    anchor = ordered[anchor_index]
    observation_ok = _contiguous(
        ordered, anchor_index, observation_bars, interval_ms=interval_ms
    )
    observed = ordered[anchor_index : anchor_index + observation_bars] if observation_ok else ()
    first_return = _pct(observed[-1].close, anchor.open) if observed else None
    high = max((row.high for row in observed), default=float("nan"))
    low = min((row.low for row in observed), default=float("nan"))
    range_pct = _pct(high, anchor.open) - _pct(low, anchor.open) if observed and _pct(high, anchor.open) is not None and _pct(low, anchor.open) is not None else None
    close_location = (
        (observed[-1].close - low) / (high - low)
        if observed and math.isfinite(high) and math.isfinite(low) and high > low
        else None
    )
    quote_sum = sum(row.quote_volume for row in observed) if observed else 0.0
    taker_sum = sum(row.taker_buy_quote_volume for row in observed) if observed else 0.0
    taker_ratio = taker_sum / quote_sum if quote_sum > 0 else None
    candidate = _candidate_side(
        first_return,
        close_location,
        taker_ratio,
        return_threshold_pct=classification_return_threshold_pct,
    )

    # Entry is the first 5m open after the 30m observation.  All forward
    # measurements begin there; the observation bars can never affect labels.
    entry_index = anchor_index + observation_bars
    entry = ordered[entry_index] if entry_index < len(ordered) else None
    returns: dict[str, float | None] = {}
    candidate_returns: dict[str, float | None] = {}
    for hours in HORIZONS_HOURS:
        exit_bar = _horizon_close(
            ordered, entry_index, hours, interval_ms=interval_ms
        ) if entry else None
        raw = _pct(exit_bar.close, entry.open) if exit_bar and entry else None
        returns[f"{hours}h"] = raw
        candidate_returns[f"{hours}h"] = (
            raw if candidate == "LONG" else (-raw if raw is not None and candidate == "SHORT" else None)
        )

    # Excursion is measured for the first 24h after entry, with gaps excluded
    # from the denominator and explicitly reported below.
    excursion_bars = 24 * 60 // bar_minutes
    excursion = ordered[entry_index : entry_index + excursion_bars] if entry else ()
    contiguous_excursion = _contiguous(
        ordered, entry_index, excursion_bars, interval_ms=interval_ms
    ) if entry else False
    if entry and contiguous_excursion and excursion:
        mfe = _pct(max(row.high for row in excursion), entry.open)
        adverse = _pct(min(row.low for row in excursion), entry.open)
        mfe_24h = max(0.0, mfe) if mfe is not None else None
        mae_24h = max(0.0, -adverse) if adverse is not None else None
    else:
        mfe_24h = None
        mae_24h = None

    window_bars = 72 * 60 // bar_minutes
    expected_times = {
        anchor.open_time_ms + offset * interval_ms
        for offset in range(window_bars)
    }
    actual_times = {row.open_time_ms for row in ordered[anchor_index : anchor_index + window_bars]}
    gaps = len(expected_times - actual_times)
    status = "OK" if observation_ok and entry is not None else "INCOMPLETE_OBSERVATION"
    return LaunchEvent(
        symbol=symbol,
        launch_time=launch_time,
        anchor_time=_as_utc(anchor.open_time_ms),
        anchor_delay_minutes=(anchor.open_time_ms - launch_ms) / 60000.0,
        status=status,
        bars_observed=len(observed),
        first_30m_return_pct=first_return,
        first_30m_range_pct=range_pct,
        first_30m_close_location=close_location,
        first_30m_taker_buy_ratio=taker_ratio,
        candidate_side=candidate,
        returns_pct=returns,
        candidate_returns_pct=candidate_returns,
        mfe_pct_24h=mfe_24h,
        mae_pct_24h=mae_24h,
        data_gap_count_72h=gaps,
    )
