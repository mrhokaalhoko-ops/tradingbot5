"""Frozen 1m launch flush/pump reversal hypothesis for research only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Iterable

from .binance_archive import KlineRow
from .launch_events import find_anchor_index
from .models import EntryKind, Side, TradePlan


ONE_MINUTE_MS = 60_000


@dataclass(frozen=True)
class LaunchReversalDecision:
    symbol: str
    status: str
    side: Side | None
    first_30m_return_pct: float | None
    plan: TradePlan | None
    confirmation_time: datetime | None = None
    reason: str = ""


def _time(value_ms: int) -> datetime:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc)


def _ordered_unique(rows: Iterable[KlineRow]) -> tuple[KlineRow, ...]:
    indexed: dict[int, KlineRow] = {}
    for row in sorted(rows, key=lambda item: item.open_time_ms):
        previous = indexed.get(row.open_time_ms)
        if previous is not None and previous != row:
            raise ValueError("conflicting duplicate launch bar")
        indexed[row.open_time_ms] = row
    return tuple(indexed[key] for key in sorted(indexed))


def _contiguous(rows: tuple[KlineRow, ...], start: int, count: int) -> bool:
    return start >= 0 and start + count <= len(rows) and all(
        rows[index].open_time_ms
        == rows[start].open_time_ms + (index - start) * ONE_MINUTE_MS
        for index in range(start, start + count)
    )


def detect_launch_reversal(
    symbol: str,
    launch_time: datetime,
    rows: Iterable[KlineRow],
    *,
    impulse_threshold_pct: float = 10.0,
    confirmation_wait_minutes: int = 60,
    hard_invalidation_range_fraction: float = 0.25,
    stop_buffer_range_fraction: float = 0.10,
    target_r: float = 2.0,
    minimum_risk_pct: float = 0.5,
    maximum_risk_pct: float = 15.0,
) -> LaunchReversalDecision:
    """Return one immutable plan formed entirely from already-closed 1m bars.

    The first 30 minutes define the observed range.  An extreme downside move
    can become a BUY only after a bullish close above the range midpoint; the
    upside case is symmetric.  Wicks outside the range do not invalidate the
    thesis, while a close 0.25 range beyond the extreme does.
    """

    if launch_time.tzinfo is None:
        raise ValueError("launch_time must be timezone-aware")
    ordered = _ordered_unique(rows)
    anchor = find_anchor_index(ordered, int(launch_time.timestamp() * 1000))
    if anchor is None or not _contiguous(ordered, anchor, 30):
        return LaunchReversalDecision(
            symbol, "INCOMPLETE_OBSERVATION", None, None, None,
            reason="No contiguous first 30 minutes",
        )
    observed = ordered[anchor : anchor + 30]
    initial_open = float(observed[0].open)
    initial_close = float(observed[-1].close)
    first_return = 100.0 * (initial_close / initial_open - 1.0)
    high = max(float(row.high) for row in observed)
    low = min(float(row.low) for row in observed)
    range_size = high - low
    if initial_open <= 0 or range_size <= 0:
        return LaunchReversalDecision(
            symbol, "INVALID_RANGE", None, first_return, None,
            reason="Non-positive initial range",
        )
    close_location = (initial_close - low) / range_size
    if first_return <= -abs(impulse_threshold_pct) and close_location <= 0.35:
        side = Side.BUY
    elif first_return >= abs(impulse_threshold_pct) and close_location >= 0.65:
        side = Side.SELL
    else:
        return LaunchReversalDecision(
            symbol, "NO_EXTREME_PATTERN", None, first_return, None,
            reason="First 30m did not form a directional extreme",
        )

    midpoint = (high + low) / 2.0
    watch_start = anchor + 30
    watch_count = max(1, int(confirmation_wait_minutes))
    if not _contiguous(ordered, watch_start, watch_count):
        return LaunchReversalDecision(
            symbol, "INCOMPLETE_CONFIRMATION", side, first_return, None,
            reason="Confirmation window contains missing 1m bars",
        )
    confirmation: KlineRow | None = None
    for row in ordered[watch_start : watch_start + watch_count]:
        candle_range = float(row.high) - float(row.low)
        close_location_1m = (
            (float(row.close) - float(row.low)) / candle_range
            if candle_range > 0 else 0.5
        )
        if side is Side.BUY:
            hard_level = low - hard_invalidation_range_fraction * range_size
            if float(row.close) < hard_level:
                return LaunchReversalDecision(
                    symbol, "HARD_INVALIDATED", side, first_return, None,
                    reason="1m close confirmed failure below launch range",
                )
            passed = (
                float(row.close) > midpoint
                and float(row.close) > float(row.open)
                and close_location_1m >= 0.60
            )
        else:
            hard_level = high + hard_invalidation_range_fraction * range_size
            if float(row.close) > hard_level:
                return LaunchReversalDecision(
                    symbol, "HARD_INVALIDATED", side, first_return, None,
                    reason="1m close confirmed failure above launch range",
                )
            passed = (
                float(row.close) < midpoint
                and float(row.close) < float(row.open)
                and close_location_1m <= 0.40
            )
        if passed:
            confirmation = row
            break
    if confirmation is None:
        return LaunchReversalDecision(
            symbol, "NO_RECLAIM", side, first_return, None,
            reason="No directional midpoint reclaim within 60 minutes",
        )

    entry = float(confirmation.close)
    if side is Side.BUY:
        stop = min(low, float(confirmation.low)) - stop_buffer_range_fraction * range_size
        risk = entry - stop
        target = entry + target_r * risk
    else:
        stop = max(high, float(confirmation.high)) + stop_buffer_range_fraction * range_size
        risk = stop - entry
        target = entry - target_r * risk
    risk_pct = 100.0 * risk / entry if entry > 0 else math.inf
    if (
        not math.isfinite(risk_pct)
        or risk_pct < minimum_risk_pct
        or risk_pct > maximum_risk_pct
        or target <= 0
    ):
        return LaunchReversalDecision(
            symbol, "INVALID_RISK_GEOMETRY", side, first_return, None,
            confirmation_time=_time(confirmation.close_time_ms),
            reason=f"Structural risk {risk_pct:.3f}% outside frozen range",
        )

    created = _time(confirmation.close_time_ms)
    key = f"{symbol}:{int(created.timestamp())}"
    plan = TradePlan(
        plan_id=f"LAUNCH_REVERSAL:{key}",
        strategy_id="FROZEN_LAUNCH_REVERSAL_V1",
        setup_id=f"LAUNCH_EXTREME:{key}",
        symbol=f"{symbol[:-4]}/USDT",
        side=side,
        created_at=created,
        valid_until=created + timedelta(minutes=2),
        exit_by=created + timedelta(hours=4),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=entry,
        stop=stop,
        target=target,
        thesis="First-30m launch extreme followed by a confirmed 1m midpoint reversal",
        score=abs(first_return),
        evidence={
            "launch_time": launch_time.isoformat(),
            "first_30m_return_pct": first_return,
            "first_30m_high": high,
            "first_30m_low": low,
            "midpoint": midpoint,
            "risk_pct": risk_pct,
            "target_r": target_r,
        },
    )
    return LaunchReversalDecision(
        symbol,
        "PLAN_CREATED",
        side,
        first_return,
        plan,
        confirmation_time=created,
        reason="Frozen midpoint reversal confirmed",
    )
