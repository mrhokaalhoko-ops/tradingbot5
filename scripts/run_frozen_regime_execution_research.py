"""Execution replay for the frozen MILD_RISK_ON continuation hypothesis."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_cross_sectional_event_study import _load
from trading_v2.data import BinanceUsdMFuturesDataSource, CausalBarStore, normalize_symbol
from trading_v2.evaluation import (
    EvaluatedPlan,
    enforce_portfolio_capacity,
    summarize,
    summarize_time_splits,
)
from trading_v2.indicators import atr
from trading_v2.models import EntryKind, Side, TradePlan
from trading_v2.replay import CausalReplayEngine


VARIANTS = ("ATR15_TIME4H", "ATR15_TARGET2R", "STRUCTURE12_TARGET2R")


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_levels(
    reference: float,
    volatility: float,
    recent_bars,
    variant: str,
) -> tuple[float, float]:
    if variant not in VARIANTS:
        raise ValueError(f"unknown execution variant: {variant}")
    if variant == "STRUCTURE12_TARGET2R":
        stop = min(float(bar.low) for bar in recent_bars[-12:]) - 0.10 * volatility
    else:
        stop = reference - 1.50 * volatility
    risk = reference - stop
    if risk <= 0:
        raise ValueError("non-positive stop distance")
    target_r = 100.0 if variant == "ATR15_TIME4H" else 2.0
    return stop, reference + target_r * risk


def _selected_events(payload: dict) -> list[dict]:
    return [
        event
        for event in payload["events"]
        if event["side"] == "BUY"
        and str(event["features"]["global_regime"]) == "MILD_RISK_ON"
        and float(event["features"]["return_24h_pct"]) >= 10.0
    ]


def _record_payload(row: EvaluatedPlan) -> dict:
    return {
        "plan_id": row.plan.plan_id,
        "symbol": row.plan.symbol,
        "created_at": row.plan.created_at.isoformat(),
        "variant": row.plan.strategy_id,
        "status": row.result.status.value,
        "filled_at": row.result.filled_at.isoformat() if row.result.filled_at else None,
        "exited_at": row.result.exited_at.isoformat() if row.result.exited_at else None,
        "net_r": row.result.net_r,
        "mfe_r": row.result.mfe_r,
        "mae_r": row.result.mae_r,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event_file", type=Path)
    parser.add_argument("--cache-dir", default="research_data/binance_usdm")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-open-positions", type=int, default=5)
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=VARIANTS,
        default=list(VARIANTS),
    )
    args = parser.parse_args()

    payload = json.loads(args.event_file.read_text(encoding="utf-8"))
    method = payload["method"]
    start = _timestamp(method["study_start"])
    end = _timestamp(method["study_end"])
    source = BinanceUsdMFuturesDataSource()
    cache = ROOT / args.cache_dir
    stores = {}
    for raw_symbol in method["symbols"]:
        symbol = normalize_symbol(raw_symbol)
        stores[symbol] = CausalBarStore(
            f"{symbol[:-4]}/USDT",
            {"5m": _load(source, cache, symbol, "5m", start, end)},
        )

    engine = CausalReplayEngine()
    records_by_variant: dict[str, list[EvaluatedPlan]] = {name: [] for name in args.variants}
    skipped = 0
    for event in _selected_events(payload):
        clean = normalize_symbol(event["symbol"])
        store = stores.get(clean)
        if store is None:
            skipped += 1
            continue
        as_of = _timestamp(event["timestamp"])
        visible = store.snapshot(as_of=as_of, history_limit=100).closed_bars("5m")
        execution = store.between("5m", as_of, as_of + timedelta(hours=4))
        if len(visible) < 20 or not execution:
            skipped += 1
            continue
        reference = float(visible[-1].close)
        volatility = float(atr(visible, 14))
        for variant in args.variants:
            stop, target = build_levels(reference, volatility, visible, variant)
            key = f"{clean}:{int(as_of.timestamp())}"
            plan = TradePlan(
                plan_id=f"{variant}:{key}",
                strategy_id=variant,
                setup_id=f"FROZEN_MILD_RISK_ON:{key}",
                symbol=store.symbol,
                side=Side.BUY,
                created_at=as_of,
                valid_until=as_of + timedelta(minutes=5),
                exit_by=as_of + timedelta(hours=4),
                entry_kind=EntryKind.MARKET_NEXT_OPEN,
                entry=reference,
                stop=stop,
                target=target,
                thesis="Frozen top-20% +10% 24h continuation in MILD_RISK_ON",
                score=float(event["features"]["return_24h_pct"]),
            )
            result = engine.replay_plan(plan, execution, horizon_end=plan.exit_by)
            records_by_variant[variant].append(EvaluatedPlan(plan=plan, result=result))

    report = {
        "source": str(args.event_file),
        "skipped_events": skipped,
        "max_open_positions": args.max_open_positions,
        "variants": {},
    }
    for variant, records in records_by_variant.items():
        selected = enforce_portfolio_capacity(
            records,
            max_open_positions=args.max_open_positions,
        )
        report["variants"][variant] = {
            "before_capacity": summarize(records),
            "capacity": {
                "rejected_capacity": selected.rejected_capacity,
                "rejected_symbol_busy": selected.rejected_symbol_busy,
            },
            "portfolio": summarize(selected.records),
            "time_splits": summarize_time_splits(selected.records, start=start, end=end),
            "records": [_record_payload(row) for row in selected.records],
        }

    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(output)
    print(json.dumps({name: row["portfolio"] for name, row in report["variants"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
