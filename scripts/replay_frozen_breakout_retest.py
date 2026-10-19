"""Exact state-machine replay of the frozen breakout retest/reclaim entry."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
from dataclasses import dataclass
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.replay_causal_rule_candidates import _episodes, _execution_month_names
from scripts.replay_frozen_aligned_breakout import (
    _report,
    candidate_side,
    load_candidates,
)
from scripts.build_causal_feature_dataset import load_symbol_frame
from trading_v2.data import CausalBarStore
from trading_v2.evaluation import EvaluatedPlan, enforce_portfolio_capacity
from trading_v2.models import Bar, EntryKind, Side, TradePlan
from trading_v2.replay import CausalReplayEngine


STRATEGY_ID = "FROZEN_BREAKOUT_RETEST_RECLAIM_V1"
RETEST_MINUTES = 90


@dataclass(frozen=True)
class RetestDecision:
    status: str
    confirmation_bar: Bar | None = None


def _close_location(bar: Bar) -> float:
    span = float(bar.high) - float(bar.low)
    return (float(bar.close) - float(bar.low)) / span if span > 0 else 0.5


def find_retest_confirmation(row, bars: tuple[Bar, ...]) -> RetestDecision:
    side = candidate_side(row)
    atr = float(row.atr)
    boundary = float(row.prior_high_24 if side is Side.BUY else row.prior_low_24)
    zone_low = boundary - 0.25 * atr
    zone_high = boundary + 0.25 * atr
    hard_level = boundary - 0.5 * atr if side is Side.BUY else boundary + 0.5 * atr
    touched = False

    for bar in bars:
        if side is Side.BUY and float(bar.close) < hard_level:
            return RetestDecision("HARD_INVALIDATED")
        if side is Side.SELL and float(bar.close) > hard_level:
            return RetestDecision("HARD_INVALIDATED")

        intersects = float(bar.low) <= zone_high and float(bar.high) >= zone_low
        touched = touched or intersects
        if not touched:
            continue

        location = _close_location(bar)
        if side is Side.BUY:
            confirmed = (
                float(bar.close) > boundary
                and float(bar.close) > float(bar.open)
                and location >= 0.60
            )
        else:
            confirmed = (
                float(bar.close) < boundary
                and float(bar.close) < float(bar.open)
                and location <= 0.40
            )
        if confirmed:
            return RetestDecision("CONFIRMED", bar)
    return RetestDecision("TOUCHED_NO_RECLAIM" if touched else "NO_TOUCH")


def build_retest_plan(row, confirmation_bar: Bar) -> TradePlan:
    side = candidate_side(row)
    atr = float(row.atr)
    boundary = float(row.prior_high_24 if side is Side.BUY else row.prior_low_24)
    reference = float(confirmation_bar.close)
    if side is Side.BUY:
        hard_level = boundary - 0.5 * atr
        stop = min(float(confirmation_bar.low), hard_level) - 0.1 * atr
        risk = reference - stop
        target = reference + 2.0 * risk
    else:
        hard_level = boundary + 0.5 * atr
        stop = max(float(confirmation_bar.high), hard_level) + 0.1 * atr
        risk = stop - reference
        target = reference - 2.0 * risk
    risk_atr = risk / atr if atr > 0 else float("inf")
    if not 0.5 <= risk_atr <= 2.0:
        raise ValueError("planned structural risk outside frozen ATR bounds")
    created = confirmation_bar.close_time
    key = f"{row.symbol}:{int(row.close_time.timestamp())}:{side.value}"
    return TradePlan(
        plan_id=f"{STRATEGY_ID}:{key}",
        strategy_id=STRATEGY_ID,
        setup_id=f"BREAKOUT_RETEST:{key}",
        symbol=confirmation_bar.symbol,
        side=side,
        created_at=created,
        valid_until=created + timedelta(minutes=5),
        exit_by=created + timedelta(hours=4),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=reference,
        stop=stop,
        target=target,
        thesis="Frozen breakout touch, close-based survival and directional reclaim",
        score=float(row.volume_ratio),
    )


def _load_store(archives_root: Path, symbol: str, group: pd.DataFrame) -> CausalBarStore | None:
    clean = str(symbol).replace("/", "").upper()
    paths = sorted((archives_root / "archives" / "5m" / clean).glob(f"{clean}-5m-*.zip"))
    available = {path.name[len(clean + "-5m-"):-4] for path in paths}
    months = [
        month for month in _execution_month_names(group, horizon_hours=6)
        if month in available
    ]
    raw = load_symbol_frame(archives_root, clean, months)
    if raw.empty:
        return None
    exchange_symbol = f"{clean[:-4]}/USDT"
    bars = tuple(
        Bar(
            symbol=exchange_symbol, timeframe="5m",
            open_time=item.open_time.to_pydatetime(), close_time=item.close_time.to_pydatetime(),
            open=float(item.open), high=float(item.high), low=float(item.low), close=float(item.close),
            volume=float(item.volume),
        )
        for item in raw.itertuples()
    )
    return CausalBarStore(exchange_symbol, {"5m": bars})


def replay(features_root: Path, archives_root: Path, metadata_path: Path, cooldown_hours: int, max_open_positions: int) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    crypto_symbols = {
        row["symbol"] for row in metadata["symbols"]
        if row["underlying_type"] == "COIN"
    }
    source = load_candidates(features_root, crypto_symbols)
    if source.empty:
        return {"method": {"strategy_id": STRATEGY_ID}, "candidate_rows": 0, "result": {}}
    episodes = _episodes(source, cooldown_hours)
    engine = CausalReplayEngine()
    state_counts: Counter[str] = Counter()
    evaluated: list[EvaluatedPlan] = []

    for symbol, group in episodes.groupby("symbol", sort=False):
        store = _load_store(archives_root, str(symbol), group)
        if store is None:
            state_counts["NO_ARCHIVE"] += len(group)
            continue
        for row in group.itertuples(index=False):
            wait_end = row.close_time.to_pydatetime() + timedelta(minutes=RETEST_MINUTES)
            waiting = store.between("5m", row.close_time.to_pydatetime(), wait_end)
            decision = find_retest_confirmation(row, waiting)
            state_counts[decision.status] += 1
            if decision.confirmation_bar is None:
                continue
            try:
                plan = build_retest_plan(row, decision.confirmation_bar)
            except ValueError:
                state_counts["REJECTED_RISK_GEOMETRY"] += 1
                continue
            execution = store.between("5m", plan.created_at, plan.exit_by)
            evaluated.append(EvaluatedPlan(
                plan=plan,
                result=engine.replay_plan(plan, execution, horizon_end=plan.exit_by),
            ))

    capacity = enforce_portfolio_capacity(evaluated, max_open_positions=max_open_positions)
    start = source["close_time"].min().normalize().to_pydatetime()
    end = (source["close_time"].max().normalize() + pd.Timedelta(days=1)).to_pydatetime()
    combined = _report(capacity.records, start=start, end=end)
    directions = {
        side.value: _report(
            tuple(row for row in capacity.records if row.plan.side is side),
            start=start, end=end,
        )
        for side in (Side.BUY, Side.SELL)
    }
    validation = combined["time_splits"]["VALIDATION"]
    test = combined["time_splits"]["TEST"]
    passed = all([
        combined["summary"]["realized"] >= 200,
        validation["expectancy_r"] is not None and validation["expectancy_r"] > 0,
        test["expectancy_r"] is not None and test["expectancy_r"] > 0,
        validation["profit_factor"] is not None and validation["profit_factor"] > 1.10,
        test["profit_factor"] is not None and test["profit_factor"] > 1.10,
        combined["stability"]["profitable_months"] >= 7,
        test["max_drawdown_r"] < 20.0,
    ])
    trade_ledger = [
        {
            "plan_id": row.plan.plan_id,
            "setup_id": row.plan.setup_id,
            "symbol": row.plan.symbol,
            "side": row.plan.side.value,
            "created_at": row.plan.created_at.isoformat(),
            "filled_at": row.result.filled_at.isoformat() if row.result.filled_at else None,
            "exited_at": row.result.exited_at.isoformat() if row.result.exited_at else None,
            "status": row.result.status.value,
            "entry_price": row.result.entry_price,
            "exit_price": row.result.exit_price,
            "gross_r": row.result.gross_r,
            "net_r": row.result.net_r,
            "mfe_r": row.result.mfe_r,
            "mae_r": row.result.mae_r,
        }
        for row in capacity.records
    ]
    return {
        "method": {
            "strategy_id": STRATEGY_ID,
            "frozen": True,
            "retest_minutes": RETEST_MINUTES,
            "zone_atr": 0.25,
            "hard_close_invalidation_atr": 0.5,
            "entry": "next 5m open after directional reclaim close",
            "target": "2R",
            "timeout": "4h after confirmation",
            "cooldown_hours": cooldown_hours,
            "max_open_positions": max_open_positions,
            "costs": "CausalReplayEngine defaults",
        },
        "candidate_rows": len(source),
        "episodes_after_cooldown": len(episodes),
        "state_counts": dict(sorted(state_counts.items())),
        "capacity": {
            "rejected_capacity": capacity.rejected_capacity,
            "rejected_symbol_busy": capacity.rejected_symbol_busy,
        },
        "result": combined,
        "directions": directions,
        "trades": trade_ledger,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--archives-root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--metadata", type=Path, default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/frozen_breakout_retest_replay.json"))
    parser.add_argument("--cooldown-hours", type=int, default=4)
    parser.add_argument("--max-open-positions", type=int, default=5)
    args = parser.parse_args()
    result = replay(args.features_root, args.archives_root, args.metadata, args.cooldown_hours, args.max_open_positions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    summary = result.get("result", {}).get("summary", {})
    print(
        f"{STRATEGY_ID}: states={result.get('state_counts')} "
        f"realized={summary.get('realized')} expectancy_r={summary.get('expectancy_r')} "
        f"profit_factor={summary.get('profit_factor')} passed={result.get('passed')}"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
