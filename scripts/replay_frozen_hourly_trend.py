"""Exact replay of a frozen slow hourly trend-transition hypothesis.

The experiment deliberately moves away from 5m micro-structure.  It is
research-only and cannot submit an order.
"""

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
from scripts.replay_frozen_aligned_breakout import _report
from scripts.replay_frozen_sweep_rejection import planned_net_rr, simulated_entry
from trading_v2.data import CausalBarStore
from trading_v2.evaluation import EvaluatedPlan, enforce_portfolio_capacity
from trading_v2.models import Bar, EntryKind, Side, TradePlan
from trading_v2.replay import CausalReplayEngine


STRATEGY_ID = "FROZEN_HOURLY_TREND_TRANSITION_V1"
MIN_NET_RR = 1.50


def select_candidates(frame: pd.DataFrame) -> pd.Series:
    """Select a fresh 1h EMA transition on the one row that closes the hour."""
    previous_alignment = frame["1h_ema_alignment"].shift(12)
    displacement_atr = (
        frame["1h_return_1_pct"].abs()
        / frame["1h_atr_pct"].where(frame["1h_atr_pct"] > 0)
    )
    hourly_close_row = frame["close_time"].dt.minute == 59
    displacement = displacement_atr.between(0.50, 2.50, inclusive="both")
    participation = frame["1h_volume_ratio"] >= 1.0
    buy = (
        (frame["1h_ema_alignment"] == 1)
        & (previous_alignment != 1)
        & (frame["1h_return_1_pct"] > 0)
        & frame["global_regime"].isin(["MILD_RISK_ON", "STRONG_RISK_ON"])
    )
    sell = (
        (frame["1h_ema_alignment"] == -1)
        & (previous_alignment != -1)
        & (frame["1h_return_1_pct"] < 0)
        & frame["global_regime"].isin(["MILD_RISK_OFF", "STRONG_RISK_OFF"])
    )
    return (
        frame["observation_valid"]
        & hourly_close_row
        & displacement
        & participation
        & (buy | sell)
    )


def candidate_side(row) -> Side:
    return Side.BUY if int(row._asdict()["h1_ema_alignment"]) == 1 else Side.SELL


def build_plan(row) -> TradePlan:
    values = row._asdict() if hasattr(row, "_asdict") else vars(row)
    side = Side.BUY if int(values["h1_ema_alignment"]) == 1 else Side.SELL
    reference = float(values["close"])
    volatility = float(values["h1_atr"])
    if reference <= 0 or volatility <= 0:
        raise ValueError("non-positive reference or 1h ATR")
    risk = 1.50 * volatility
    stop = reference - risk if side is Side.BUY else reference + risk
    target = reference + 2.0 * risk if side is Side.BUY else reference - 2.0 * risk
    created = values["close_time"].to_pydatetime()
    key = f"{values['symbol']}:{int(created.timestamp())}:{side.value}"
    return TradePlan(
        plan_id=f"{STRATEGY_ID}:{key}",
        strategy_id=STRATEGY_ID,
        setup_id=f"HOURLY_TREND_TRANSITION:{key}",
        symbol=f"{str(values['symbol'])[:-4]}/USDT",
        side=side,
        created_at=created,
        valid_until=created + timedelta(minutes=5),
        exit_by=created + timedelta(hours=48),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=reference,
        stop=stop,
        target=target,
        thesis="Fresh hourly EMA20/50 trend transition in matching BTC/ETH regime",
        score=float(values["h1_volume_ratio"]),
    )


def load_candidates(features_root: Path, crypto_symbols: set[str]) -> pd.DataFrame:
    columns = [
        "close_time", "symbol", "close", "observation_valid", "global_regime",
        "1h_return_1_pct", "1h_atr", "1h_atr_pct", "1h_volume_ratio",
        "1h_ema_alignment",
    ]
    frames = []
    for path in sorted(features_root.glob("*/*.parquet")):
        if path.stem not in crypto_symbols:
            continue
        frame = pd.read_parquet(path, columns=columns)
        chosen = frame[select_candidates(frame)]
        if not chosen.empty:
            frames.append(chosen.rename(columns={
                "1h_atr": "h1_atr",
                "1h_volume_ratio": "h1_volume_ratio",
                "1h_ema_alignment": "h1_ema_alignment",
            }))
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True).sort_values("close_time")


def _store_for_group(
    archives_root: Path, symbol: str, group: pd.DataFrame
) -> CausalBarStore | None:
    clean = str(symbol).replace("/", "").upper()
    paths = sorted((archives_root / "archives" / "5m" / clean).glob(f"{clean}-5m-*.zip"))
    available = {path.name[len(clean + "-5m-"):-4] for path in paths}
    months = [
        month for month in _execution_month_names(group, horizon_hours=50)
        if month in available
    ]
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
            open=float(row.open), high=float(row.high), low=float(row.low),
            close=float(row.close), volume=float(row.volume),
        )
        for row in raw.itertuples()
    )
    return CausalBarStore(exchange_symbol, {"5m": bars})


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
    source = load_candidates(features_root, crypto_symbols)
    if source.empty:
        return {"method": {"strategy_id": STRATEGY_ID}, "candidate_rows": 0, "result": {}}
    episodes = _episodes(source, cooldown_hours)
    engine = CausalReplayEngine()
    evaluated: list[EvaluatedPlan] = []
    rejected_execution_rr = 0

    for symbol, group in episodes.groupby("symbol", sort=False):
        store = _store_for_group(archives_root, str(symbol), group)
        if store is None:
            continue
        for row in group.itertuples(index=False):
            plan = build_plan(row)
            bars = store.between("5m", plan.created_at, plan.exit_by)
            if not bars:
                continue
            entry = simulated_entry(float(bars[0].open), plan.side, engine.costs)
            rr = planned_net_rr(
                entry=entry, stop=plan.stop, target=plan.target,
                side=plan.side, costs=engine.costs,
            )
            if rr < MIN_NET_RR:
                rejected_execution_rr += 1
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
            "signal": "fresh hourly EMA20/50 alignment on completed 1h candle",
            "trigger": "0.5-2.5 ATR directional 1h move with volume ratio >=1",
            "regime": "BTC/ETH risk regime must match direction",
            "entry": "next 5m open with modeled slippage",
            "stop": "1.5 completed-1h ATR",
            "target": "2R",
            "minimum_execution_net_rr": MIN_NET_RR,
            "timeout": "48h",
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
        "rejected_execution_net_rr": rejected_execution_rr,
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
        default=Path("research_data/causal_futures_dataset/frozen_hourly_trend_replay.json"),
    )
    parser.add_argument("--cooldown-hours", type=int, default=48)
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
