"""Replay a frozen multi-bar failed-breakout reversal hypothesis.

This is the final hand-written counterfactual derived from the rejected
aligned-breakout study.  It has no live or Demo execution authority.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.replay_causal_rule_candidates import _episodes
from scripts.replay_frozen_aligned_breakout import (
    _report,
    _store_for_group,
    candidate_side as breakout_side,
    load_candidates,
)
from scripts.replay_frozen_sweep_rejection import planned_net_rr, simulated_entry
from trading_v2.evaluation import EvaluatedPlan, enforce_portfolio_capacity
from trading_v2.models import Bar, EntryKind, Side, TradePlan
from trading_v2.replay import CausalReplayEngine


STRATEGY_ID = "FROZEN_FAILED_BREAKOUT_REVERSAL_V1"
CONFIRMATION_BARS = 3
MIN_NET_RR = 1.50


@dataclass(frozen=True)
class FailureDecision:
    status: str
    confirmation: Bar | None = None
    extreme: float | None = None


def _close_location(bar: Bar) -> float:
    span = float(bar.high) - float(bar.low)
    return (float(bar.close) - float(bar.low)) / span if span > 0 else 0.5


def find_failed_breakout(row, bars: tuple[Bar, ...]) -> FailureDecision:
    """Require a later directional close back inside the broken range."""
    original = breakout_side(row)
    boundary = float(row.prior_high_24 if original is Side.BUY else row.prior_low_24)
    atr = float(row.atr)
    extreme = float(row.high if original is Side.BUY else row.low)
    for bar in bars[:CONFIRMATION_BARS]:
        if original is Side.BUY:
            extreme = max(extreme, float(bar.high))
            if float(bar.close) > boundary + atr:
                return FailureDecision("HARD_INVALIDATED", extreme=extreme)
            confirmed = (
                float(bar.close) < boundary
                and float(bar.close) < float(bar.open)
                and _close_location(bar) <= 0.40
            )
        else:
            extreme = min(extreme, float(bar.low))
            if float(bar.close) < boundary - atr:
                return FailureDecision("HARD_INVALIDATED", extreme=extreme)
            confirmed = (
                float(bar.close) > boundary
                and float(bar.close) > float(bar.open)
                and _close_location(bar) >= 0.60
            )
        if confirmed:
            return FailureDecision("CONFIRMED", confirmation=bar, extreme=extreme)
    return FailureDecision("NO_FAILURE_CONFIRMATION", extreme=extreme)


def build_plan(row, decision: FailureDecision) -> TradePlan:
    if decision.confirmation is None or decision.extreme is None:
        raise ValueError("failed breakout is not confirmed")
    original = breakout_side(row)
    side = Side.SELL if original is Side.BUY else Side.BUY
    reference = float(decision.confirmation.close)
    atr = float(row.atr)
    midpoint = (float(row.prior_high_24) + float(row.prior_low_24)) / 2.0
    stop = (
        float(decision.extreme) + 0.10 * atr
        if side is Side.SELL
        else float(decision.extreme) - 0.10 * atr
    )
    risk_atr = abs(reference - stop) / atr if atr > 0 else float("inf")
    if not 0.25 <= risk_atr <= 1.50:
        raise ValueError("planned structural risk outside frozen ATR bounds")
    if (side is Side.BUY and not stop < reference < midpoint) or (
        side is Side.SELL and not midpoint < reference < stop
    ):
        raise ValueError("range midpoint does not provide directional room")
    created = decision.confirmation.close_time
    key = f"{row.symbol}:{int(created.timestamp())}:{side.value}"
    return TradePlan(
        plan_id=f"{STRATEGY_ID}:{key}",
        strategy_id=STRATEGY_ID,
        setup_id=f"FAILED_BREAKOUT:{key}",
        symbol=decision.confirmation.symbol,
        side=side,
        created_at=created,
        valid_until=created + timedelta(minutes=5),
        exit_by=created + timedelta(hours=4),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=reference,
        stop=stop,
        target=midpoint,
        thesis="Aligned breakout failed by closing back inside the prior 2h range",
        score=float(row.volume_ratio),
    )


def replay(
    features_root: Path,
    archives_root: Path,
    metadata_path: Path,
    cooldown_hours: int,
    max_open_positions: int,
) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    crypto_symbols = {
        row["symbol"] for row in metadata["symbols"]
        if row["underlying_type"] == "COIN"
    }
    # The reversal is frozen for a RANGE macro regime only.  Fading a breakout
    # in a strong matching global regime is a separate, untested hypothesis.
    source = load_candidates(features_root, crypto_symbols)
    source = source[source["global_regime"] == "RANGE"].copy()
    if source.empty:
        return {"method": {"strategy_id": STRATEGY_ID}, "candidate_rows": 0, "result": {}}
    episodes = _episodes(source, cooldown_hours)
    engine = CausalReplayEngine()
    states: Counter[str] = Counter()
    rejected_geometry = 0
    rejected_rr = 0
    evaluated: list[EvaluatedPlan] = []

    for symbol, group in episodes.groupby("symbol", sort=False):
        store = _store_for_group(archives_root, str(symbol), group)
        if store is None:
            states["NO_ARCHIVE"] += len(group)
            continue
        for row in group.itertuples(index=False):
            confirmation_end = row.close_time.to_pydatetime() + timedelta(
                minutes=5 * CONFIRMATION_BARS
            )
            waiting = store.between(
                "5m", row.close_time.to_pydatetime(), confirmation_end
            )
            decision = find_failed_breakout(row, waiting)
            states[decision.status] += 1
            if decision.confirmation is None:
                continue
            try:
                plan = build_plan(row, decision)
            except ValueError:
                rejected_geometry += 1
                continue
            bars = store.between("5m", plan.created_at, plan.exit_by)
            if not bars:
                continue
            entry = simulated_entry(float(bars[0].open), plan.side, engine.costs)
            rr = planned_net_rr(
                entry=entry, stop=plan.stop, target=plan.target,
                side=plan.side, costs=engine.costs,
            )
            if rr < MIN_NET_RR:
                rejected_rr += 1
                continue
            evaluated.append(EvaluatedPlan(
                plan=plan,
                result=engine.replay_plan(plan, bars, horizon_end=plan.exit_by),
            ))

    capacity = enforce_portfolio_capacity(
        evaluated, max_open_positions=max_open_positions
    )
    start = source["close_time"].min().normalize().to_pydatetime()
    end = (source["close_time"].max().normalize() + pd.Timedelta(days=1)).to_pydatetime()
    combined = _report(capacity.records, start=start, end=end)
    validation = combined["time_splits"]["VALIDATION"]
    test = combined["time_splits"]["TEST"]
    passed = all([
        combined["summary"]["realized"] >= 100,
        validation["realized"] >= 20,
        test["realized"] >= 20,
        validation["expectancy_r"] is not None and validation["expectancy_r"] > 0,
        test["expectancy_r"] is not None and test["expectancy_r"] > 0,
        validation["profit_factor"] is not None and validation["profit_factor"] > 1.10,
        test["profit_factor"] is not None and test["profit_factor"] > 1.10,
        combined["stability"]["profitable_months"] >= 7,
        test["max_drawdown_r"] < 20.0,
    ])
    return {
        "method": {
            "strategy_id": STRATEGY_ID,
            "frozen": True,
            "parent_signal": "FROZEN_ALIGNED_BREAKOUT_V1 in RANGE only",
            "confirmation": "within 3 closed 5m bars, directional close back in range",
            "hard_invalidation": "close 1 ATR beyond broken boundary",
            "entry": "next 5m open with modeled slippage",
            "stop": "0.1 ATR beyond post-breakout excursion",
            "target": "midpoint of preceding 24 completed 5m bars",
            "minimum_execution_net_rr": MIN_NET_RR,
            "timeout": "4h",
            "cooldown_hours": cooldown_hours,
            "max_open_positions": max_open_positions,
            "costs": engine.costs.__dict__,
            "pass_criteria": (
                ">=100 realized and >=20 per holdout; positive expectancy and PF>1.10 "
                "in VALIDATION and TEST; >=7 profitable months; TEST drawdown<20R"
            ),
            "authority": "RESEARCH_ONLY",
        },
        "candidate_rows": len(source),
        "episodes_after_cooldown": len(episodes),
        "state_counts": dict(sorted(states.items())),
        "rejected_risk_geometry": rejected_geometry,
        "rejected_execution_net_rr": rejected_rr,
        "capacity": {
            "rejected_capacity": capacity.rejected_capacity,
            "rejected_symbol_busy": capacity.rejected_symbol_busy,
        },
        "result": combined,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features-root", type=Path,
        default=Path("research_data/causal_futures_dataset/features"),
    )
    parser.add_argument(
        "--archives-root", type=Path,
        default=Path("research_data/causal_futures_dataset"),
    )
    parser.add_argument(
        "--metadata", type=Path,
        default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("research_data/causal_futures_dataset/frozen_failed_breakout_replay.json"),
    )
    parser.add_argument("--cooldown-hours", type=int, default=4)
    parser.add_argument("--max-open-positions", type=int, default=5)
    args = parser.parse_args()
    result = replay(
        args.features_root, args.archives_root, args.metadata,
        args.cooldown_hours, args.max_open_positions,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(args.output)
    summary = result.get("result", {}).get("summary", {})
    print(
        f"{STRATEGY_ID}: candidates={result.get('candidate_rows')} "
        f"realized={summary.get('realized')} expectancy_r={summary.get('expectancy_r')} "
        f"profit_factor={summary.get('profit_factor')} passed={result.get('passed')}"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
