"""Run the exact production momentum path as an order-free causal replay."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines.ta_engine import TAEngine
from strategy.filter_profiles import FILTER_PROFILES
from trading_v2.data import BinanceUsdMFuturesDataSource, CausalBarStore, normalize_symbol
from trading_v2.evaluation import (
    enforce_portfolio_capacity,
    summarize,
    summarize_splits,
    summarize_time_splits,
)
from trading_v2.momentum_research import (
    MomentumResearchConfig,
    evaluate_production_momentum,
)
from trading_v2.research import GlobalRegimeTimeline


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a timezone")
    return parsed


def _load(source, cache, symbol, timeframe, start, end):
    key = (
        f"{normalize_symbol(symbol)}_{timeframe}_{int(start.timestamp())}_"
        f"{int(end.timestamp())}.jsonl.gz"
    )
    path = cache / key
    if not path.exists():
        bars = source.fetch(
            symbol=symbol, timeframe=timeframe, start=start, end=end
        )
        source.write_cache(path, bars)
    return source.read_cache(path)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _record_payload(row):
    plan = row.plan
    result = row.result
    return {
        "plan_id": plan.plan_id,
        "setup_id": plan.setup_id,
        "symbol": plan.symbol,
        "side": plan.side.value,
        "created_at": plan.created_at.isoformat(),
        "entry": plan.entry,
        "stop": plan.stop,
        "target": plan.target,
        "planned_rr": plan.planned_rr,
        "regime": plan.evidence.get("regime"),
        "target_type": plan.evidence.get("target_type"),
        "status": result.status.value,
        "filled_at": result.filled_at.isoformat() if result.filled_at else None,
        "exited_at": result.exited_at.isoformat() if result.exited_at else None,
        "net_r": result.net_r,
        "mfe_r": result.mfe_r,
        "mae_r": result.mae_r,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Production momentum causal replay")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", type=_time, required=True)
    parser.add_argument("--end", type=_time, required=True)
    parser.add_argument("--cache-dir", default="research_data/binance_usdm")
    parser.add_argument(
        "--output", default="research_data/momentum_production_research.json"
    )
    parser.add_argument("--max-wait-bars", type=int, default=3)
    parser.add_argument("--hot-1m-pct", type=float, default=0.8)
    parser.add_argument("--hot-5m-pct", type=float, default=2.0)
    parser.add_argument("--impulse-min-volume-ratio", type=float, default=1.30)
    parser.add_argument("--impulse-min-range-atr", type=float, default=0.80)
    parser.add_argument("--impulse-max-range-atr", type=float, default=2.50)
    parser.add_argument("--confirmation-max-extension-atr", type=float, default=1.25)
    parser.add_argument("--confirmation-min-volume-ratio", type=float, default=0.75)
    parser.add_argument("--max-open-positions", type=int, default=5)
    parser.add_argument(
        "--policy",
        choices=("production_all_gates", "scored_v2"),
        default="production_all_gates",
    )
    args = parser.parse_args()

    source = BinanceUsdMFuturesDataSource()
    cache = ROOT / args.cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    symbols = list(dict.fromkeys(["BTCUSDT", "ETHUSDT", *args.symbols]))
    stores = {}
    for symbol in symbols:
        clean = normalize_symbol(symbol)
        unified = f"{clean[:-4]}/USDT"
        stores[clean] = CausalBarStore(
            unified,
            {
                timeframe: _load(
                    source, cache, symbol, timeframe, args.start, args.end
                )
                for timeframe in ("1m", "5m", "15m", "1h")
            },
        )

    regime = GlobalRegimeTimeline(stores["BTCUSDT"], stores["ETHUSDT"])
    profile = dict(FILTER_PROFILES["smc_balanced"])
    ta_engine = TAEngine()
    research_config = MomentumResearchConfig(
        impulse_policy=args.policy,
        confirmation_policy=args.policy,
        max_wait_bars=max(1, int(args.max_wait_bars)),
        hot_momentum_1m_pct=max(0.1, float(args.hot_1m_pct)),
        hot_momentum_5m_pct=max(0.2, float(args.hot_5m_pct)),
        min_volume_ratio=max(1.0, float(args.impulse_min_volume_ratio)),
        min_range_atr=max(0.1, float(args.impulse_min_range_atr)),
        max_range_atr=max(0.2, float(args.impulse_max_range_atr)),
        confirmation_max_extension_atr=max(
            0.25, float(args.confirmation_max_extension_atr)
        ),
        confirmation_min_volume_ratio=max(
            0.0, float(args.confirmation_min_volume_ratio)
        ),
    )
    runs = {}
    combined = []
    for symbol in args.symbols:
        clean = normalize_symbol(symbol)
        run = evaluate_production_momentum(
            stores[clean],
            start=args.start,
            end=args.end,
            regime_at=regime.at,
            ta_engine=ta_engine,
            profile=profile,
            config=research_config,
        )
        combined.extend(run.records)
        runs[clean] = {
            "funnel": dict(run.funnel),
            "metadata": dict(run.metadata),
            "summary": summarize(run.records),
            "splits": summarize_splits(run.records),
        }

    combined = tuple(sorted(combined, key=lambda row: row.plan.created_at))
    selection = enforce_portfolio_capacity(
        combined,
        max_open_positions=args.max_open_positions,
    )
    capacity_time_splits = summarize_time_splits(
        selection.records,
        start=args.start,
        end=args.end,
    )
    test_metrics = capacity_time_splits["TEST"]
    evidence_sufficient = int(test_metrics.get("realized") or 0) >= 30
    stable_positive = bool(
        evidence_sufficient
        and (test_metrics.get("expectancy_r") or 0) > 0
        and (test_metrics.get("profit_factor") or 0) > 1.1
    )
    output_payload = {
        "period": {"start": args.start.isoformat(), "end": args.end.isoformat()},
        "symbols": [normalize_symbol(symbol) for symbol in args.symbols],
        "runs": runs,
        "combined": {
            "summary": summarize(combined),
            "splits": summarize_splits(combined),
            "time_splits": summarize_time_splits(
                combined,
                start=args.start,
                end=args.end,
            ),
            "records": [_record_payload(row) for row in combined],
            "capacity_adjusted": {
                "summary": summarize(selection.records),
                "time_splits": capacity_time_splits,
                "records": [_record_payload(row) for row in selection.records],
                "max_open_positions": args.max_open_positions,
                "rejected_capacity": selection.rejected_capacity,
                "rejected_symbol_busy": selection.rejected_symbol_busy,
            },
        },
        "promotion_assessment": {
            "replay_approved": False,
            "evidence_sufficient": evidence_sufficient,
            "stable_positive_test": stable_positive,
            "minimum_test_realized_required": 30,
            "note": (
                "This report never changes production execution settings. "
                "Full-market ranking and additional regimes remain required."
            ),
        },
        "research_config": research_config.__dict__,
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = _json_safe(output_payload)
    output.write_text(
        json.dumps(safe_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(output)
    print(json.dumps(safe_payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
