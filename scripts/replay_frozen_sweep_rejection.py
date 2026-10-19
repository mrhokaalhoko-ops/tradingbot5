"""Exact replay of one pre-registered liquidity-sweep rejection hypothesis.

This is research-only.  The rules in this file are frozen before the result is
inspected and this module has no order or live-execution authority.
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

from scripts.replay_causal_rule_candidates import _episodes, _execution_month_names
from scripts.build_causal_feature_dataset import load_symbol_frame
from scripts.replay_frozen_aligned_breakout import _report
from trading_v2.data import CausalBarStore
from trading_v2.evaluation import EvaluatedPlan, enforce_portfolio_capacity
from trading_v2.models import Bar, EntryKind, Side, TradePlan
from trading_v2.replay import CausalReplayEngine, ReplayCosts


STRATEGY_ID = "FROZEN_SWEEP_REJECTION_V1"
MIN_NET_RR = 1.50


def select_candidates(frame: pd.DataFrame) -> pd.Series:
    """Select confirmed failed breaks using closed 5m evidence only."""
    atr = frame["atr"].where(frame["atr"] > 0)
    low_sweep_depth_atr = (frame["prior_low_24"] - frame["low"]) / atr
    high_sweep_depth_atr = (frame["high"] - frame["prior_high_24"]) / atr
    usable_regime = frame["global_regime"] != "HIGH_VOLATILITY_EVENT"
    enough_participation = frame["volume_ratio"] >= 1.20

    buy = (
        (frame["wick_sweep_low_24"] == 1)
        & (frame["close"] > frame["open"])
        & (frame["close_location"] >= 0.65)
        & low_sweep_depth_atr.between(0.10, 1.00, inclusive="both")
        & ~(
            (frame["15m_ema_alignment"] == -1)
            & (frame["1h_ema_alignment"] == -1)
        )
        & ~frame["global_regime"].isin(["STRONG_RISK_OFF"])
    )
    sell = (
        (frame["wick_sweep_high_24"] == 1)
        & (frame["close"] < frame["open"])
        & (frame["close_location"] <= 0.35)
        & high_sweep_depth_atr.between(0.10, 1.00, inclusive="both")
        & ~(
            (frame["15m_ema_alignment"] == 1)
            & (frame["1h_ema_alignment"] == 1)
        )
        & ~frame["global_regime"].isin(["STRONG_RISK_ON"])
    )
    return frame["observation_valid"] & usable_regime & enough_participation & (buy | sell)


def candidate_side(row) -> Side:
    return Side.BUY if int(row.wick_sweep_low_24) == 1 else Side.SELL


def structural_geometry(row) -> tuple[Side, float, float]:
    """Return side, structural stop and the known two-hour range midpoint."""
    side = candidate_side(row)
    atr = float(row.atr)
    if atr <= 0:
        raise ValueError("non-positive ATR")
    target = (float(row.prior_high_24) + float(row.prior_low_24)) / 2.0
    stop = (
        float(row.low) - 0.10 * atr
        if side is Side.BUY
        else float(row.high) + 0.10 * atr
    )
    return side, stop, target


def simulated_entry(open_price: float, side: Side, costs: ReplayCosts) -> float:
    slip = float(costs.entry_slippage_bps) / 10_000.0
    return float(open_price) * (1.0 + slip if side is Side.BUY else 1.0 - slip)


def planned_net_rr(
    *, entry: float, stop: float, target: float, side: Side, costs: ReplayCosts
) -> float:
    """Net target R available at the causally observable execution open."""
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return float("-inf")
    if side is Side.BUY:
        if not stop < entry < target:
            return float("-inf")
        target_fill = target * (1.0 - float(costs.target_slippage_bps) / 10_000.0)
        gross_reward = target_fill - entry
    else:
        if not target < entry < stop:
            return float("-inf")
        target_fill = target * (1.0 + float(costs.target_slippage_bps) / 10_000.0)
        gross_reward = entry - target_fill
    fees = (
        entry * float(costs.entry_fee_bps)
        + target_fill * float(costs.exit_fee_bps)
    ) / 10_000.0
    return (gross_reward - fees) / risk


def build_plan(row) -> TradePlan:
    side, stop, target = structural_geometry(row)
    reference = float(row.close)
    risk_atr = abs(reference - stop) / float(row.atr)
    if not 0.25 <= risk_atr <= 1.50:
        raise ValueError("planned structural risk outside frozen ATR bounds")
    if (side is Side.BUY and not stop < reference < target) or (
        side is Side.SELL and not target < reference < stop
    ):
        raise ValueError("range midpoint does not provide directional room")
    created = row.close_time.to_pydatetime()
    key = f"{row.symbol}:{int(created.timestamp())}:{side.value}"
    return TradePlan(
        plan_id=f"{STRATEGY_ID}:{key}",
        strategy_id=STRATEGY_ID,
        setup_id=f"SWEEP_REJECTION:{key}",
        symbol=f"{str(row.symbol)[:-4]}/USDT",
        side=side,
        created_at=created,
        valid_until=created + timedelta(minutes=5),
        exit_by=created + timedelta(hours=4),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=reference,
        stop=stop,
        target=target,
        thesis="Closed 5m failed break with structural stop and range-midpoint target",
        score=float(row.volume_ratio),
    )


def load_candidates(features_root: Path, crypto_symbols: set[str]) -> pd.DataFrame:
    columns = [
        "close_time", "symbol", "global_regime", "observation_valid",
        "open", "high", "low", "close", "atr", "volume_ratio",
        "close_location", "15m_ema_alignment", "1h_ema_alignment",
        "prior_high_24", "prior_low_24", "wick_sweep_high_24",
        "wick_sweep_low_24",
    ]
    frames = []
    for path in sorted(features_root.glob("*/*.parquet")):
        if path.stem not in crypto_symbols:
            continue
        frame = pd.read_parquet(path, columns=columns)
        selected = frame[select_candidates(frame)]
        if not selected.empty:
            frames.append(selected)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True).sort_values("close_time")


def _store_for_group(
    archives_root: Path, symbol: str, group: pd.DataFrame
) -> CausalBarStore | None:
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
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
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
    rejected_risk_geometry = 0
    rejected_execution_rr = 0

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
            if not bars:
                continue
            entry = simulated_entry(float(bars[0].open), plan.side, engine.costs)
            net_rr = planned_net_rr(
                entry=entry,
                stop=plan.stop,
                target=plan.target,
                side=plan.side,
                costs=engine.costs,
            )
            if net_rr < MIN_NET_RR:
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
            "signal": "closed 5m 0.1-1.0 ATR sweep and directional rejection",
            "participation": "5m quote-volume ratio >=1.20",
            "trend_policy": "reject only when both 15m and 1h align against side",
            "regime_policy": "reject strong global regime against side and high-vol event",
            "entry": "next 5m open with modeled slippage",
            "stop": "0.1 ATR beyond sweep wick; reference risk 0.25-1.5 ATR",
            "target": "midpoint of preceding 24 completed 5m bars",
            "minimum_execution_net_rr": MIN_NET_RR,
            "timeout": "4h",
            "cooldown_hours": cooldown_hours,
            "max_open_positions": max_open_positions,
            "costs": engine.costs.__dict__,
            "pass_criteria": (
                ">=200 realized; positive expectancy and PF>1.10 in VALIDATION and TEST; "
                ">=7 profitable months; TEST drawdown<20R"
            ),
            "authority": "RESEARCH_ONLY",
        },
        "candidate_rows": len(source),
        "episodes_after_cooldown": len(episodes),
        "rejected_risk_geometry": rejected_risk_geometry,
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
        default=Path("research_data/causal_futures_dataset/frozen_sweep_rejection_replay.json"),
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
