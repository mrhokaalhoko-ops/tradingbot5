"""Causal strategy evaluation helpers shared by CLI research and tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .evaluation import EvaluatedPlan, summarize_splits
from .interfaces import TradingStrategy
from .regime import classify_global_regime
from .replay import CausalReplayEngine
from .data import CausalBarStore


@dataclass(frozen=True)
class GlobalRegimeTimeline:
    btc: CausalBarStore
    eth: CausalBarStore

    def at(self, as_of: datetime) -> str:
        btc = self.btc.snapshot(as_of=as_of, history_limit=80).closed_bars("1h")
        eth = self.eth.snapshot(as_of=as_of, history_limit=80).closed_bars("1h")
        return classify_global_regime(btc, eth)


def evaluate_strategy(
    strategy: TradingStrategy,
    store: CausalBarStore,
    *,
    start: datetime,
    end: datetime,
    regime_at: Callable[[datetime], str],
    decision_timeframe: str = "5m",
    execution_timeframe: str = "1m",
    replay: CausalReplayEngine | None = None,
) -> tuple[EvaluatedPlan, ...]:
    """Evaluate every closed decision bar without exposing future candles."""
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("start/end must be aware and ordered")
    engine = replay or CausalReplayEngine()
    rows: list[EvaluatedPlan] = []
    seen_plan_ids: set[str] = set()
    busy_until: datetime | None = None
    decision_bars = store.between(decision_timeframe, start, end)
    for decision_bar in decision_bars:
        as_of = decision_bar.close_time
        if as_of < start or as_of > end:
            continue
        if busy_until is not None and as_of <= busy_until:
            continue
        snapshot = store.snapshot(
            as_of=as_of,
            regime=regime_at(as_of),
            history_limit=300,
        )
        for plan in strategy.evaluate(snapshot):
            if plan.plan_id in seen_plan_ids:
                continue
            seen_plan_ids.add(plan.plan_id)
            execution_bars = store.between(
                execution_timeframe,
                plan.created_at,
                min(plan.exit_by, end),
            )
            result = engine.replay_plan(plan, execution_bars, horizon_end=end)
            rows.append(EvaluatedPlan(plan=plan, result=result))
            # A known fill with an unknown exit still occupies the position
            # through its planned holding window, not only its entry deadline.
            busy_until = result.exited_at or (
                plan.exit_by if result.filled_at is not None else plan.valid_until
            )
            break
    return tuple(rows)


def compare_strategies(
    strategies: Iterable[TradingStrategy],
    store: CausalBarStore,
    *,
    start: datetime,
    end: datetime,
    regime_at: Callable[[datetime], str],
) -> dict:
    output = {}
    for strategy in strategies:
        rows = evaluate_strategy(
            strategy,
            store,
            start=start,
            end=end,
            regime_at=regime_at,
        )
        output[strategy.strategy_id] = summarize_splits(rows)
    return output
