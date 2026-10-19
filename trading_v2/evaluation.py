"""Chronological evaluation and transparent R-based performance metrics."""

from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
import math
from datetime import datetime
from statistics import median
from typing import Iterable

from .models import ReplayResult, ReplayStatus, TradePlan


@dataclass(frozen=True)
class EvaluatedPlan:
    plan: TradePlan
    result: ReplayResult


@dataclass(frozen=True)
class PortfolioSelection:
    """Plans admitted after replaying portfolio-level position constraints."""

    records: tuple[EvaluatedPlan, ...]
    rejected_capacity: int = 0
    rejected_symbol_busy: int = 0


def _validate_split_ratios(train_ratio: float, validation_ratio: float) -> None:
    if not 0 < train_ratio < 1 or not 0 <= validation_ratio < 1:
        raise ValueError("invalid chronological split ratios")
    if train_ratio + validation_ratio >= 1:
        raise ValueError("train + validation must leave a test segment")


def chronological_split(
    records: Iterable[EvaluatedPlan],
    *,
    train_ratio: float = 0.60,
    validation_ratio: float = 0.20,
) -> dict[str, tuple[EvaluatedPlan, ...]]:
    rows = tuple(sorted(records, key=lambda row: row.plan.created_at))
    _validate_split_ratios(train_ratio, validation_ratio)
    train_end = int(len(rows) * train_ratio)
    validation_end = train_end + int(len(rows) * validation_ratio)
    return {
        "TRAIN": rows[:train_end],
        "VALIDATION": rows[train_end:validation_end],
        "TEST": rows[validation_end:],
    }


def chronological_time_split(
    records: Iterable[EvaluatedPlan],
    *,
    start: datetime,
    end: datetime,
    train_ratio: float = 0.60,
    validation_ratio: float = 0.20,
) -> dict[str, tuple[EvaluatedPlan, ...]]:
    """Split by shared calendar boundaries rather than each symbol's row count.

    A single pair of timestamps must be used for every symbol/strategy in a
    portfolio experiment.  This prevents a sparse symbol's "validation" rows
    from occurring during another symbol's training period.
    """

    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("start/end must be timezone-aware and ordered")
    _validate_split_ratios(train_ratio, validation_ratio)
    rows = tuple(sorted(records, key=lambda row: row.plan.created_at))
    if any(row.plan.created_at < start or row.plan.created_at > end for row in rows):
        raise ValueError("record timestamp outside evaluation window")

    duration = end - start
    train_end = start + duration * train_ratio
    validation_end = start + duration * (train_ratio + validation_ratio)
    return {
        "TRAIN": tuple(row for row in rows if row.plan.created_at < train_end),
        "VALIDATION": tuple(
            row
            for row in rows
            if train_end <= row.plan.created_at < validation_end
        ),
        "TEST": tuple(row for row in rows if row.plan.created_at >= validation_end),
    }


def enforce_portfolio_capacity(
    records: Iterable[EvaluatedPlan],
    *,
    max_open_positions: int = 5,
) -> PortfolioSelection:
    """Replay actual fills through one portfolio-wide concurrency gate.

    Unfilled plans do not consume a position slot.  Filled plans are admitted
    at their historical fill time, with higher-scored simultaneous candidates
    considered first.  This models the live max-position and one-position-per-
    symbol rules without using future candles to alter the trading signal.
    """

    if (
        isinstance(max_open_positions, bool)
        or not isinstance(max_open_positions, int)
        or max_open_positions < 1
    ):
        raise ValueError("max_open_positions must be a positive integer")

    rows = tuple(records)
    ordered = sorted(
        rows,
        key=lambda row: (
            row.result.filled_at or row.plan.created_at,
            0 if row.result.filled_at is not None else 1,
            -float(row.plan.score),
            row.plan.created_at,
            row.plan.symbol,
            row.plan.plan_id,
        ),
    )
    accepted: list[EvaluatedPlan] = []
    active: list[tuple[datetime, str]] = []
    rejected_capacity = 0
    rejected_symbol_busy = 0

    for row in ordered:
        filled_at = row.result.filled_at
        if filled_at is None:
            accepted.append(row)
            continue

        active = [item for item in active if item[0] > filled_at]
        if any(symbol == row.plan.symbol for _, symbol in active):
            rejected_symbol_busy += 1
            continue
        if len(active) >= max_open_positions:
            rejected_capacity += 1
            continue

        position_end = row.result.exited_at or row.plan.exit_by
        if position_end < filled_at:
            position_end = filled_at
        active.append((position_end, row.plan.symbol))
        accepted.append(row)

    return PortfolioSelection(
        records=tuple(sorted(accepted, key=lambda row: row.plan.created_at)),
        rejected_capacity=rejected_capacity,
        rejected_symbol_busy=rejected_symbol_busy,
    )


def summarize(records: Iterable[EvaluatedPlan]) -> dict:
    # Creation order is retained for plan cohorts. Realized equity below is
    # ordered separately by actual recorded exit timestamps.
    rows = tuple(sorted(
        records,
        key=lambda row: (row.plan.created_at, row.plan.plan_id),
    ))
    realized_statuses = {
        ReplayStatus.TP_FIRST,
        ReplayStatus.SL_FIRST,
        ReplayStatus.OPEN_AT_HORIZON,
    }
    realized = [
        row for row in rows
        if row.result.status in realized_statuses and row.result.net_r is not None
    ]
    net_rs = [float(row.result.net_r) for row in realized]
    if any(not math.isfinite(value) for value in net_rs):
        raise ValueError("realized returns must be finite")
    gross_rs = [
        float(row.result.gross_r)
        for row in realized
        if row.result.gross_r is not None
    ]
    cost_drags = [
        float(row.result.gross_r) - float(row.result.net_r)
        for row in realized
        if row.result.gross_r is not None and row.result.net_r is not None
    ]
    mfe_rs = [
        float(row.result.mfe_r)
        for row in realized
        if row.result.mfe_r is not None
    ]
    mae_rs = [
        float(row.result.mae_r)
        for row in realized
        if row.result.mae_r is not None
    ]
    winners = [value for value in net_rs if value > 0]
    losers = [value for value in net_rs if value <= 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))

    missing_exit_times = sum(row.result.exited_at is None for row in realized)
    max_drawdown = None if missing_exit_times else 0.0
    if not missing_exit_times:
        # Simultaneous closes form one observation; alphabetical plan order
        # must not create fictitious intermediate equity peaks or troughs.
        closed_pnl = defaultdict(float)
        for row in realized:
            closed_pnl[row.result.exited_at] += float(row.result.net_r)
        equity = peak = 0.0
        for closed_at in sorted(closed_pnl):
            equity += closed_pnl[closed_at]
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

    by_status = {
        status.value: sum(row.result.status is status for row in rows)
        for status in ReplayStatus
        if any(row.result.status is status for row in rows)
    }
    return {
        "plans": len(rows),
        "realized": len(realized),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": len(winners) / len(realized) if realized else None,
        "expectancy_r": sum(net_rs) / len(net_rs) if net_rs else None,
        "gross_expectancy_r": sum(gross_rs) / len(gross_rs) if gross_rs else None,
        "average_cost_drag_r": sum(cost_drags) / len(cost_drags) if cost_drags else None,
        "median_r": median(net_rs) if net_rs else None,
        "average_win_r": sum(winners) / len(winners) if winners else None,
        "average_loss_r": sum(losers) / len(losers) if losers else None,
        "payoff_ratio": (
            (sum(winners) / len(winners)) / abs(sum(losers) / len(losers))
            if winners and losers and sum(losers) != 0
            else None
        ),
        "median_mfe_r": median(mfe_rs) if mfe_rs else None,
        "median_mae_r": median(mae_rs) if mae_rs else None,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (float("inf") if gross_profit > 0 else None)
        ),
        "max_drawdown_r": max_drawdown,
        "drawdown_basis": "REALIZED_PNL_GROUPED_BY_RECORDED_EXIT_TIME",
        "drawdown_missing_exit_times": missing_exit_times,
        "incomplete": sum(row.result.status is ReplayStatus.INCOMPLETE_DATA for row in rows),
        "total_net_r": sum(net_rs),
        "fill_rate": len(realized) / len(rows) if rows else None,
        "ambiguous": sum(
            row.result.status in {
                ReplayStatus.AMBIGUOUS_ENTRY_BAR,
                ReplayStatus.AMBIGUOUS_SAME_BAR,
            }
            for row in rows
        ),
        "by_status": by_status,
    }


def summarize_splits(records: Iterable[EvaluatedPlan]) -> dict:
    splits = chronological_split(records)
    return {name: summarize(rows) for name, rows in splits.items()}


def summarize_time_splits(
    records: Iterable[EvaluatedPlan],
    *,
    start: datetime,
    end: datetime,
    train_ratio: float = 0.60,
    validation_ratio: float = 0.20,
) -> dict:
    splits = chronological_time_split(
        records,
        start=start,
        end=end,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
    )
    return {name: summarize(rows) for name, rows in splits.items()}


def summarize_by_calendar_month(records: Iterable[EvaluatedPlan]) -> dict[str, dict]:
    """Summarize results by UTC calendar month without changing chronology.

    Monthly stability is a stricter diagnostic than one full-period total: a
    strategy that earns all of its profit in one isolated month is not treated
    as a durable edge merely because the aggregate is positive.
    """

    groups: dict[str, list[EvaluatedPlan]] = {}
    for row in sorted(records, key=lambda item: item.plan.created_at):
        created = row.plan.created_at
        if created.tzinfo is None:
            raise ValueError("plan created_at must be timezone-aware")
        key = created.strftime("%Y-%m")
        groups.setdefault(key, []).append(row)
    return {key: summarize(rows) for key, rows in groups.items()}


def summarize_by_symbol(records: Iterable[EvaluatedPlan]) -> dict[str, dict]:
    """Expose cross-symbol concentration instead of hiding it in totals."""

    groups: dict[str, list[EvaluatedPlan]] = {}
    for row in records:
        groups.setdefault(row.plan.symbol, []).append(row)
    return {
        symbol: summarize(rows)
        for symbol, rows in sorted(groups.items())
    }
