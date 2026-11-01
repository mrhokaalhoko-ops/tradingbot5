"""Replay the frozen long-horizon Donchian/ATR research hypothesis."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_v2.long_horizon_trend import (  # noqa: E402
    LongHorizonTrendPolicy,
    TrendTrade,
    aggregate_hourly_to_4h,
    apply_portfolio_capacity,
    simulate_symbol,
)
from trading_v2.binance_archive import parse_kline_archive  # noqa: E402


def load_hourly(root: Path, symbol: str) -> pd.DataFrame:
    rows = []
    for path in sorted((root / "archives" / "1h" / symbol).glob("*.zip")):
        for row in parse_kline_archive(path):
            rows.append(
                {
                    "open_time": pd.to_datetime(row.open_time_ms, unit="ms", utc=True),
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                    "volume": row.base_volume,
                }
            )
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).sort_values("open_time")
    duplicate_count = int(frame.duplicated("open_time", keep=False).sum())
    if duplicate_count:
        raise ValueError(f"{symbol} has {duplicate_count} duplicate hourly rows")
    return frame.reset_index(drop=True)


def contiguous_hourly_segments(frame: pd.DataFrame) -> tuple[pd.DataFrame, ...]:
    if frame.empty:
        return ()
    ordered = frame.sort_values("open_time").reset_index(drop=True).copy()
    boundary = ordered["open_time"].diff().ne(pd.Timedelta(hours=1))
    boundary.iloc[0] = True
    ordered["_segment"] = boundary.cumsum()
    return tuple(
        segment.drop(columns="_segment").reset_index(drop=True)
        for _, segment in ordered.groupby("_segment", sort=True)
    )


def split_name(entry_time: str) -> str:
    timestamp = pd.Timestamp(entry_time)
    if timestamp < pd.Timestamp("2024-01-01", tz="UTC"):
        return "DEVELOPMENT"
    if timestamp < pd.Timestamp("2025-01-01", tz="UTC"):
        return "VALIDATION"
    return "HOLDOUT"


def metrics(trades: list[TrendTrade] | tuple[TrendTrade, ...]) -> dict:
    ordered = sorted(trades, key=lambda row: (row.exit_time, row.symbol))
    values = [float(row.net_r) for row in ordered]
    positive = sum(value for value in values if value > 0)
    negative = -sum(value for value in values if value < 0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "mean_net_r": sum(values) / len(values) if values else None,
        "total_net_r": sum(values),
        "profit_factor": positive / negative if negative else None,
        "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "max_drawdown_r": drawdown,
        "median_holding_hours": (
            float(pd.Series([row.holding_hours for row in ordered]).median())
            if ordered
            else None
        ),
        "fee_drag_r": sum(row.fee_drag_r for row in ordered),
        "funding_drag_r": sum(row.funding_drag_r for row in ordered),
    }


def group_metrics(trades: tuple[TrendTrade, ...], key_fn) -> dict:
    grouped: dict[str, list[TrendTrade]] = {}
    for trade in trades:
        grouped.setdefault(str(key_fn(trade)), []).append(trade)
    return {key: metrics(value) for key, value in sorted(grouped.items())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("research_data/long_horizon_hourly"),
    )
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--max-open-positions", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    data_root = args.data_root if args.data_root.is_absolute() else ROOT / args.data_root
    manifest = json.loads((data_root / "manifest.json").read_text(encoding="utf-8"))
    symbols = tuple(sorted(set(args.symbols or manifest["symbols"])))
    policy = LongHorizonTrendPolicy()
    all_trades: list[TrendTrade] = []
    coverage: dict[str, dict] = {}
    for symbol in symbols:
        hourly = load_hourly(data_root, symbol)
        if hourly.empty:
            coverage[symbol] = {"status": "MISSING"}
            continue
        segments = contiguous_hourly_segments(hourly)
        four_hour_segments = [aggregate_hourly_to_4h(segment) for segment in segments]
        trades = tuple(
            trade
            for segment in four_hour_segments
            for trade in simulate_symbol(symbol, segment, policy)
        )
        all_trades.extend(trades)
        coverage[symbol] = {
            "status": "AVAILABLE",
            "hourly_bars": len(hourly),
            "hourly_segments": len(segments),
            "hourly_gaps": max(0, len(segments) - 1),
            "four_hour_bars": sum(len(segment) for segment in four_hour_segments),
            "first_open": hourly.open_time.min().isoformat(),
            "last_open": hourly.open_time.max().isoformat(),
            "independent_trades": len(trades),
        }
        print(
            f"{symbol}: {len(hourly)} hourly bars, {len(segments)} segments, "
            f"{len(trades)} trades",
            flush=True,
        )

    accepted = apply_portfolio_capacity(tuple(all_trades), args.max_open_positions)
    split_metrics = group_metrics(accepted, lambda row: split_name(row.entry_time))
    validation = split_metrics.get("VALIDATION", {})
    holdout = split_metrics.get("HOLDOUT", {})
    promotion_checks = {
        "validation_sample": int(validation.get("trades", 0)) >= 20,
        "validation_expectancy": float(validation.get("mean_net_r") or 0) > 0,
        "validation_profit_factor": float(validation.get("profit_factor") or 0) > 1.10,
        "holdout_sample": int(holdout.get("trades", 0)) >= 30,
        "holdout_expectancy": float(holdout.get("mean_net_r") or 0) > 0,
        "holdout_profit_factor": float(holdout.get("profit_factor") or 0) > 1.10,
    }
    payload = {
        "research_status": "PRELIMINARY_OFFLINE_RESEARCH_ONLY",
        "execution_authority": False,
        "strict_r2b_eligible": False,
        "dataset_id": manifest["dataset_id"],
        "policy": asdict(policy),
        "policy_hash": policy.policy_hash,
        "symbols": list(symbols),
        "max_open_positions": args.max_open_positions,
        "coverage": coverage,
        "independent_signal_trades": len(all_trades),
        "portfolio_trades": len(accepted),
        "overall": metrics(accepted),
        "time_splits": split_metrics,
        "years": group_metrics(accepted, lambda row: pd.Timestamp(row.entry_time).year),
        "sides": group_metrics(accepted, lambda row: row.side),
        "symbol_metrics": group_metrics(accepted, lambda row: row.symbol),
        "promotion_checks": promotion_checks,
        "eligible_for_forward_shadow_review": all(promotion_checks.values()),
        "limitations": [
            "Binance Vision archive timestamps do not prove first availability.",
            "Historical tickSize provenance is absent; raw trade prices are not rounded retroactively.",
            "Funding is a fixed adverse sensitivity charge, not reconstructed historical funding.",
            "The fixed major-symbol universe has survivorship and listing-history limitations.",
            "A positive result would authorize review only, never execution.",
        ],
        "trades": [asdict(row) for row in accepted],
    }
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"overall": payload["overall"], "splits": split_metrics, "checks": promotion_checks}, indent=2))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
