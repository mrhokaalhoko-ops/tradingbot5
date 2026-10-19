"""Exact 5m replay of the two pre-declared regime candidates."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_causal_feature_dataset import load_symbol_frame
from scripts.evaluate_causal_rules import RULES
from trading_v2.data import CausalBarStore
from trading_v2.evaluation import (
    EvaluatedPlan,
    enforce_portfolio_capacity,
    summarize,
    summarize_by_calendar_month,
    summarize_by_symbol,
    summarize_time_splits,
)
from trading_v2.models import EntryKind, Side, TradePlan
from trading_v2.replay import CausalReplayEngine


def _episodes(frame: pd.DataFrame, cooldown_hours: int) -> pd.DataFrame:
    selected = []
    for _, group in frame.sort_values(["symbol", "close_time"]).groupby("symbol", sort=False):
        last = None
        for row in group.itertuples(index=False):
            if last is not None and row.close_time < last + timedelta(hours=cooldown_hours):
                continue
            selected.append(row._asdict())
            last = row.close_time
    return pd.DataFrame(selected, columns=frame.columns)


def _plan(row, name: str) -> TradePlan:
    side = Side.BUY if "BUY" in name else Side.SELL
    reference = float(row.close)
    volatility = float(row.atr)
    if volatility <= 0:
        raise ValueError("candidate has non-positive ATR")
    stop = reference - volatility if side is Side.BUY else reference + volatility
    target = reference + 2.0 * volatility if side is Side.BUY else reference - 2.0 * volatility
    created = row.close_time.to_pydatetime()
    key = f"{row.symbol}:{int(created.timestamp())}"
    return TradePlan(
        plan_id=f"{name}:{key}",
        strategy_id=name,
        setup_id=f"CAUSAL_RULE:{key}",
        symbol=f"{row.symbol[:-4]}/USDT",
        side=side,
        created_at=created,
        valid_until=created + timedelta(minutes=5),
        exit_by=created + timedelta(hours=4),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=reference,
        stop=stop,
        target=target,
        thesis="Pre-declared global-regime candidate; exact 5m replay only",
        score=abs(float(row.return_24_pct)),
    )


def _load_candidate_rows(features_root: Path) -> pd.DataFrame:
    columns = [
        "open_time", "close_time", "symbol", "global_regime", "return_24_pct",
        "close", "atr", "observation_valid",
    ]
    frames = []
    for path in sorted(features_root.glob("*/*.parquet")):
        frame = pd.read_parquet(path, columns=columns)
        frames.append(frame[
            frame["observation_valid"]
            & (frame["open_time"].dt.minute == 55)
            & (frame["open_time"].dt.second == 0)
        ])
    return pd.concat(frames, ignore_index=True)


def _execution_month_names(group: pd.DataFrame, horizon_hours: int = 4) -> list[str]:
    """Load only months intersecting a candidate's forward replay window."""

    months: set[str] = set()
    for value in group["close_time"]:
        timestamp = pd.Timestamp(value)
        months.add(timestamp.strftime("%Y-%m"))
        months.add((timestamp + pd.Timedelta(hours=horizon_hours)).strftime("%Y-%m"))
    return sorted(months)


def replay(features_root: Path, archives_root: Path, cooldown_hours: int, max_open_positions: int) -> dict:
    source = _load_candidate_rows(features_root)
    engine = CausalReplayEngine()
    output = {}
    for name, selector in RULES.items():
        selected = _episodes(source[selector(source)].copy(), cooldown_hours)
        evaluated: list[EvaluatedPlan] = []
        for symbol, group in selected.groupby("symbol", sort=False):
            clean = str(symbol).replace("/", "").upper()
            paths = sorted((archives_root / "archives" / "5m" / clean).glob(f"{clean}-5m-*.zip"))
            if not paths:
                continue
            available = {path.name[len(clean + "-5m-"):-4] for path in paths}
            month_names = [
                month for month in _execution_month_names(group)
                if month in available
            ]
            raw = load_symbol_frame(archives_root, clean, month_names)
            if raw.empty:
                continue
            raw["symbol"] = f"{clean[:-4]}/USDT"
            # Constructing from load_symbol_frame avoids reparsing into dicts;
            # convert its rows to the typed bars required by the replay engine.
            from trading_v2.models import Bar
            bars = tuple(
                Bar(
                    symbol=raw["symbol"].iloc[0], timeframe="5m",
                    open_time=row.open_time.to_pydatetime(), close_time=row.close_time.to_pydatetime(),
                    open=float(row.open), high=float(row.high), low=float(row.low), close=float(row.close),
                    volume=float(row.volume),
                )
                for row in raw.itertuples()
            )
            store = CausalBarStore(raw["symbol"].iloc[0], {"5m": bars})
            for row in group.itertuples(index=False):
                try:
                    plan = _plan(row, name)
                except ValueError:
                    continue
                execution = store.between("5m", plan.created_at, plan.exit_by)
                result = engine.replay_plan(plan, execution, horizon_end=plan.exit_by)
                evaluated.append(EvaluatedPlan(plan=plan, result=result))
        capacity = enforce_portfolio_capacity(evaluated, max_open_positions=max_open_positions)
        monthly = summarize_by_calendar_month(capacity.records)
        symbols = summarize_by_symbol(capacity.records)
        realized_months = [row for row in monthly.values() if row["realized"]]
        realized_symbols = [row for row in symbols.values() if row["realized"]]
        output[name] = {
            "candidates_after_cooldown": len(evaluated),
            "before_capacity": summarize(evaluated),
            "capacity": {
                "rejected_capacity": capacity.rejected_capacity,
                "rejected_symbol_busy": capacity.rejected_symbol_busy,
            },
            "portfolio": summarize(capacity.records),
            "stability": {
                "calendar_months": monthly,
                "profitable_months": sum(
                    float(row["total_net_r"]) > 0 for row in realized_months
                ),
                "realized_months": len(realized_months),
                "symbols": symbols,
                "profitable_symbols": sum(
                    float(row["total_net_r"]) > 0 for row in realized_symbols
                ),
                "realized_symbols": len(realized_symbols),
            },
            "time_splits": summarize_time_splits(
                capacity.records,
                start=source["open_time"].min().normalize().to_pydatetime(),
                end=(source["open_time"].max().normalize() + pd.Timedelta(days=1)).to_pydatetime(),
            ),
        }
    return {
        "method": {
            "entry": "next 5m open",
            "stop": "1 ATR from decision close",
            "target": "2 ATR from decision close",
            "exit": "4h time exit",
            "costs": "CausalReplayEngine defaults (fees + entry/stop/target/time slippage)",
            "cooldown_hours": cooldown_hours,
            "max_open_positions": max_open_positions,
            "note": "Exact OHLC replay; same-bar stop/target remains ambiguous, never a win.",
        },
        "rules": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--archives-root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/exact_rule_replay.json"))
    parser.add_argument("--cooldown-hours", type=int, default=4)
    parser.add_argument("--max-open-positions", type=int, default=5)
    args = parser.parse_args()
    result = replay(args.features_root, args.archives_root, args.cooldown_hours, args.max_open_positions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    # Keep CLI output compact and compatible with Windows legacy code pages;
    # the complete Unicode symbol breakdown remains in the UTF-8 JSON file.
    for name, row in result["rules"].items():
        portfolio = row["portfolio"]
        stability = row["stability"]
        print(
            f"{name}: plans={portfolio['plans']} "
            f"expectancy_r={portfolio['expectancy_r']} "
            f"profit_factor={portfolio['profit_factor']} "
            f"profitable_months={stability['profitable_months']}/"
            f"{stability['realized_months']}"
        )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
