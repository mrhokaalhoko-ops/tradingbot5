"""One-shot exact replay of the pre-registered aligned-breakout hypothesis."""

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
from scripts.replay_causal_rule_candidates import _episodes, _execution_month_names
from trading_v2.data import CausalBarStore
from trading_v2.evaluation import (
    EvaluatedPlan,
    enforce_portfolio_capacity,
    summarize,
    summarize_by_calendar_month,
    summarize_by_symbol,
    summarize_time_splits,
)
from trading_v2.models import Bar, EntryKind, Side, TradePlan
from trading_v2.replay import CausalReplayEngine


STRATEGY_ID = "FROZEN_ALIGNED_BREAKOUT_V1"


def select_candidates(frame: pd.DataFrame) -> pd.Series:
    valid_regime = frame["global_regime"] != "HIGH_VOLATILITY_EVENT"
    body_displacement = frame["body_pct"] >= 0.5 * frame["atr_pct"]
    long_mask = (
        (frame["breakout_up_24"] == 1)
        & (frame["ema_alignment"] == 1)
        & (frame["15m_ema_alignment"] == 1)
        & (frame["1h_ema_alignment"] == 1)
        & (frame["volume_ratio"] >= 1.5)
        & body_displacement
        & (frame["close_location"] >= 0.70)
        & frame["ema20_distance_atr"].between(0.0, 2.0, inclusive="both")
        & ~frame["global_regime"].isin(["STRONG_RISK_OFF"])
    )
    short_mask = (
        (frame["breakdown_down_24"] == 1)
        & (frame["ema_alignment"] == -1)
        & (frame["15m_ema_alignment"] == -1)
        & (frame["1h_ema_alignment"] == -1)
        & (frame["volume_ratio"] >= 1.5)
        & body_displacement
        & (frame["close_location"] <= 0.30)
        & frame["ema20_distance_atr"].between(-2.0, 0.0, inclusive="both")
        & ~frame["global_regime"].isin(["STRONG_RISK_ON"])
    )
    return frame["observation_valid"] & valid_regime & (long_mask | short_mask)


def candidate_side(row) -> Side:
    return Side.BUY if int(row.breakout_up_24) == 1 else Side.SELL


def build_plan(row) -> TradePlan:
    side = candidate_side(row)
    reference = float(row.close)
    atr = float(row.atr)
    if atr <= 0:
        raise ValueError("non-positive ATR")
    if side is Side.BUY:
        stop = min(float(row.low), float(row.prior_high_24)) - 0.1 * atr
        risk = reference - stop
        target = reference + 2.0 * risk
    else:
        stop = max(float(row.high), float(row.prior_low_24)) + 0.1 * atr
        risk = stop - reference
        target = reference - 2.0 * risk
    risk_atr = risk / atr
    if not 0.5 <= risk_atr <= 2.0:
        raise ValueError("planned structural risk outside frozen ATR bounds")
    created = row.close_time.to_pydatetime()
    direction = side.value
    key = f"{row.symbol}:{int(created.timestamp())}:{direction}"
    return TradePlan(
        plan_id=f"{STRATEGY_ID}:{key}",
        strategy_id=STRATEGY_ID,
        setup_id=f"ALIGNED_BREAKOUT:{key}",
        symbol=f"{str(row.symbol)[:-4]}/USDT",
        side=side,
        created_at=created,
        valid_until=created + timedelta(minutes=5),
        exit_by=created + timedelta(hours=4),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=reference,
        stop=stop,
        target=target,
        thesis="Frozen multi-timeframe volume-backed structural breakout",
        score=float(row.volume_ratio),
    )


def load_candidates(features_root: Path, crypto_symbols: set[str]) -> pd.DataFrame:
    columns = [
        "close_time", "symbol", "global_regime", "observation_valid",
        "open", "high", "low", "close", "atr", "atr_pct", "body_pct",
        "volume_ratio", "close_location", "ema20_distance_atr", "ema_alignment",
        "15m_ema_alignment", "1h_ema_alignment", "prior_high_24", "prior_low_24",
        "breakout_up_24", "breakdown_down_24",
    ]
    frames = []
    for path in sorted(features_root.glob("*/*.parquet")):
        if path.stem not in crypto_symbols:
            continue
        frame = pd.read_parquet(path, columns=columns)
        chosen = frame[select_candidates(frame)]
        if not chosen.empty:
            frames.append(chosen)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True).sort_values("close_time")


def _store_for_group(archives_root: Path, symbol: str, group: pd.DataFrame) -> CausalBarStore | None:
    clean = str(symbol).replace("/", "").upper()
    paths = sorted((archives_root / "archives" / "5m" / clean).glob(f"{clean}-5m-*.zip"))
    available = {path.name[len(clean + "-5m-"):-4] for path in paths}
    months = [month for month in _execution_month_names(group) if month in available]
    raw = load_symbol_frame(archives_root, clean, months)
    if raw.empty:
        return None
    exchange_symbol = f"{clean[:-4]}/USDT"
    bars = tuple(
        Bar(
            symbol=exchange_symbol,
            timeframe="5m",
            open_time=row.open_time.to_pydatetime(),
            close_time=row.close_time.to_pydatetime(),
            open=float(row.open), high=float(row.high), low=float(row.low), close=float(row.close),
            volume=float(row.volume),
        )
        for row in raw.itertuples()
    )
    return CausalBarStore(exchange_symbol, {"5m": bars})


def _report(records: tuple[EvaluatedPlan, ...], *, start, end) -> dict:
    months = summarize_by_calendar_month(records)
    symbols = summarize_by_symbol(records)
    realized_months = [row for row in months.values() if row["realized"]]
    realized_symbols = [row for row in symbols.values() if row["realized"]]
    return {
        "summary": summarize(records),
        "time_splits": summarize_time_splits(records, start=start, end=end),
        "stability": {
            "calendar_months": months,
            "profitable_months": sum(float(row["total_net_r"]) > 0 for row in realized_months),
            "realized_months": len(realized_months),
            "symbols": symbols,
            "profitable_symbols": sum(float(row["total_net_r"]) > 0 for row in realized_symbols),
            "realized_symbols": len(realized_symbols),
        },
    }


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
    evaluated: list[EvaluatedPlan] = []
    rejected_risk_geometry = 0
    for symbol, group in episodes.groupby("symbol", sort=False):
        store = _store_for_group(archives_root, str(symbol), group)
        if store is None:
            continue
        for row in group.itertuples(index=False):
            try:
                plan = build_plan(row)
            except ValueError:
                rejected_risk_geometry += 1
                continue
            bars = store.between("5m", plan.created_at, plan.exit_by)
            evaluated.append(EvaluatedPlan(
                plan=plan,
                result=engine.replay_plan(plan, bars, horizon_end=plan.exit_by),
            ))
    capacity = enforce_portfolio_capacity(evaluated, max_open_positions=max_open_positions)
    start = source["close_time"].min().normalize().to_pydatetime()
    end = (source["close_time"].max().normalize() + pd.Timedelta(days=1)).to_pydatetime()
    combined = _report(capacity.records, start=start, end=end)
    directions = {
        side.value: _report(
            tuple(row for row in capacity.records if row.plan.side is side),
            start=start,
            end=end,
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
    return {
        "method": {
            "strategy_id": STRATEGY_ID,
            "frozen": True,
            "entry": "next 5m open",
            "stop": "0.1 ATR beyond breakout candle/prior boundary; 0.5-2 ATR risk",
            "target": "2R",
            "timeout": "4h",
            "cooldown_hours": cooldown_hours,
            "max_open_positions": max_open_positions,
            "costs": "CausalReplayEngine defaults",
            "pass_criteria": (
                ">=200 realized; positive expectancy and PF>1.10 in VALIDATION and TEST; "
                ">=7 profitable months; TEST drawdown<20R"
            ),
        },
        "candidate_rows": len(source),
        "episodes_after_cooldown": len(episodes),
        "rejected_risk_geometry": rejected_risk_geometry,
        "capacity": {
            "rejected_capacity": capacity.rejected_capacity,
            "rejected_symbol_busy": capacity.rejected_symbol_busy,
        },
        "result": combined,
        "directions": directions,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--archives-root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--metadata", type=Path, default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/frozen_aligned_breakout_replay.json"))
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
        f"{STRATEGY_ID}: candidates={result.get('candidate_rows')} "
        f"realized={summary.get('realized')} expectancy_r={summary.get('expectancy_r')} "
        f"profit_factor={summary.get('profit_factor')} passed={result.get('passed')}"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
