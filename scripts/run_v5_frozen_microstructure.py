"""Run the frozen V5 aggregate-trade microstructure experiment.

This module has research authority only.  It evaluates the protocol frozen in
``V5_MICROSTRUCTURE_FROZEN_PROTOCOL_2026-08-31.md`` and never emits orders.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


TARGET = "future_return_15m_pct"
PREDICTION_MONTHS = ("2026-06", "2026-07")
MIN_PREDICTED_MOVE_PCT = 0.20
ROUND_TRIP_COST_BPS = 12.0
HOLD_MINUTES = 15
MAX_OPEN_POSITIONS = 5

# Explicit allow-list: no price level, symbol identity, future value,
# availability flag, timestamp or segment identifier can enter the model.
FEATURES = [
    "aggregate_trades",
    "total_notional_5m",
    "buy_notional_5m",
    "sell_notional_5m",
    "signed_notional_5m",
    "last_1m_imbalance",
    "imbalance_5m",
    "largest_trade_share_5m",
    "return_5m_bps",
    "imbalance_15m",
    "return_15m_bps",
    "volume_z_6h",
    "flow_z_6h",
    "flow_price_agreement_bps",
    "continuation_score",
    "absorption_score",
    "price_response_bps_per_million",
    "xrank_imbalance_5m",
    "xrank_imbalance_15m",
    "xrank_flow_z_6h",
    "xrank_volume_z_6h",
    "xrank_return_5m_bps",
    "xrank_return_15m_bps",
    "xrank_absorption_score",
    "xrank_continuation_score",
    "xrank_largest_trade_share_5m",
    "xrank_total_notional_5m",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def training_bounds(month: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    if month == "2026-06":
        return (
            pd.Timestamp("2026-05-01T00:00:00Z"),
            pd.Timestamp("2026-05-31T00:00:00Z"),
        )
    if month == "2026-07":
        return (
            pd.Timestamp("2026-06-01T00:00:00Z"),
            pd.Timestamp("2026-06-30T00:00:00Z"),
        )
    raise ValueError(f"month is outside frozen V5 protocol: {month}")


def build_model() -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        (
            "model",
            HistGradientBoostingRegressor(
                max_depth=3,
                learning_rate=0.05,
                max_iter=100,
                l2_regularization=10.0,
                random_state=42,
            ),
        ),
    ])


def load_samples(features_root: Path) -> tuple[pd.DataFrame, dict]:
    paths = {
        path.stem.removeprefix("v5_microstructure_"): path
        for path in features_root.glob("v5_microstructure_*.parquet")
    }
    required = {"2026-05", "2026-06", "2026-07"}
    missing = sorted(required - paths.keys())
    if missing:
        raise FileNotFoundError(f"missing V5 feature partitions: {missing}")
    columns = [
        "decision_time", "symbol", "feature_ready",
        "primary_label_available", TARGET, *FEATURES,
    ]
    frames = [pd.read_parquet(paths[month], columns=columns) for month in sorted(required)]
    samples = pd.concat(frames, ignore_index=True)
    samples["decision_time"] = pd.to_datetime(samples["decision_time"], utc=True)
    samples = samples.sort_values(["decision_time", "symbol"]).reset_index(drop=True)

    duplicate_rows = int(samples.duplicated(["decision_time", "symbol"]).sum())
    forbidden = [
        name for name in FEATURES
        if name.startswith("future_")
        or name in {"symbol", "feature_ready", "primary_label_available"}
    ]
    if duplicate_rows or forbidden:
        raise ValueError(
            f"invalid V5 evidence: duplicate_rows={duplicate_rows}, forbidden={forbidden}"
        )
    audit = {
        "rows": int(len(samples)),
        "duplicate_symbol_times": duplicate_rows,
        "feature_allow_list": list(FEATURES),
        "forbidden_features": forbidden,
        "partitions": {
            month: {
                "path": str(paths[month]),
                "sha256": sha256_file(paths[month]),
                "rows": int((samples["decision_time"].dt.strftime("%Y-%m") == month).sum()),
            }
            for month in sorted(required)
        },
    }
    return samples, audit


def walk_forward_predictions(samples: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    predictions = []
    diagnostics = {}
    for month in PREDICTION_MONTHS:
        train_start, train_end = training_bounds(month)
        predict_start = pd.Timestamp(f"{month}-01T00:00:00Z")
        predict_end = predict_start + pd.offsets.MonthBegin(1)
        train = samples[
            (samples["decision_time"] >= train_start)
            & (samples["decision_time"] < train_end)
            & samples["feature_ready"]
            & samples["primary_label_available"]
        ].copy()
        future = samples[
            (samples["decision_time"] >= predict_start)
            & (samples["decision_time"] < predict_end)
            & samples["feature_ready"]
            & samples["primary_label_available"]
        ].copy()
        if train.empty or future.empty:
            raise RuntimeError(f"insufficient frozen V5 data for {month}")
        model = build_model()
        model.fit(train[FEATURES], train[TARGET])
        future["prediction_pct"] = model.predict(future[FEATURES])
        future["evaluation_split"] = "VALIDATION" if month == "2026-06" else "TEST"
        future["model_month"] = month
        predictions.append(future)
        diagnostics[month] = {
            "train_start": train_start.isoformat(),
            "train_end_exclusive": train_end.isoformat(),
            "embargo_day": train_end.strftime("%Y-%m-%d"),
            "train_rows": int(len(train)),
            "prediction_rows": int(len(future)),
            "all_row_prediction_correlation": float(
                future["prediction_pct"].corr(future[TARGET])
            ),
            "prediction_mean_pct": float(future["prediction_pct"].mean()),
            "prediction_std_pct": float(future["prediction_pct"].std()),
        }
    return pd.concat(predictions, ignore_index=True).sort_values(
        ["decision_time", "symbol"]
    ), diagnostics


def select_portfolio(predictions: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    active: list[tuple[pd.Timestamp, str]] = []
    accepted: list[dict] = []
    counters: Counter[str] = Counter()
    for timestamp, group in predictions.groupby("decision_time", sort=True):
        active = [item for item in active if item[0] > timestamp]
        long_rows = group[group["prediction_pct"] >= MIN_PREDICTED_MOVE_PCT]
        short_rows = group[group["prediction_pct"] <= -MIN_PREDICTED_MOVE_PCT]
        candidates = []
        if not long_rows.empty:
            candidates.append(long_rows.nlargest(1, "prediction_pct").iloc[0])
        if not short_rows.empty:
            candidates.append(short_rows.nsmallest(1, "prediction_pct").iloc[0])
        counters["decision_times"] += 1
        counters["directional_candidates"] += len(candidates)
        for row in sorted(
            candidates,
            key=lambda item: abs(float(item["prediction_pct"])),
            reverse=True,
        ):
            symbol = str(row["symbol"])
            if any(active_symbol == symbol for _, active_symbol in active):
                counters["rejected_symbol_busy"] += 1
                continue
            if len(active) >= MAX_OPEN_POSITIONS:
                counters["rejected_capacity"] += 1
                continue
            accepted.append(row.to_dict())
            active.append((timestamp + pd.Timedelta(minutes=HOLD_MINUTES), symbol))
            counters["accepted"] += 1
    selected = pd.DataFrame(accepted)
    if selected.empty:
        return selected, dict(counters)
    selected["side"] = np.where(selected["prediction_pct"] >= 0, "LONG", "SHORT")
    direction = np.where(selected["side"] == "LONG", 1.0, -1.0)
    selected["gross_bps"] = direction * selected[TARGET] * 100.0
    selected["net_bps"] = selected["gross_bps"] - ROUND_TRIP_COST_BPS
    selected["exit_time"] = selected["decision_time"] + pd.Timedelta(minutes=HOLD_MINUTES)
    return selected.sort_values(["decision_time", "symbol"]), dict(counters)


def _profit_factor(values: pd.Series) -> float | None:
    winners = values[values > 0]
    losers = values[values <= 0]
    profit = float(winners.sum())
    loss = abs(float(losers.sum()))
    if loss > 0:
        return profit / loss
    return float("inf") if profit > 0 else None


def summarize_month(frame: pd.DataFrame, all_rows: pd.DataFrame) -> dict:
    if frame.empty:
        return {"positions": 0, "passed": False}
    net = frame["net_bps"].astype(float)
    positive = net[net > 0]
    positive_total = float(positive.sum())
    concentration = (
        float(positive.max() / positive_total)
        if positive_total > 0 and not positive.empty else None
    )
    sides = {
        side: {
            "positions": int(len(group)),
            "mean_net_bps": float(group["net_bps"].mean()),
        }
        for side, group in frame.groupby("side", sort=True)
    }
    all_row_correlation = float(
        all_rows["prediction_pct"].corr(all_rows[TARGET])
    )
    position_correlation = float(
        frame["prediction_pct"].corr(frame[TARGET])
    )
    gates = {
        "at_least_300_positions": len(frame) >= 300,
        "positive_net_mean": float(net.mean()) > 0,
        "profit_factor_above_1_10": (_profit_factor(net) or 0.0) > 1.10,
        "positive_prediction_correlation": all_row_correlation > 0,
        "both_directions_non_negative": (
            set(sides) == {"LONG", "SHORT"}
            and all(row["mean_net_bps"] >= 0 for row in sides.values())
        ),
        "single_winner_below_10pct": (
            concentration is not None and concentration < 0.10
        ),
    }
    return {
        "positions": int(len(frame)),
        "mean_gross_bps": float(frame["gross_bps"].mean()),
        "mean_net_bps": float(net.mean()),
        "median_net_bps": float(net.median()),
        "win_rate_after_cost": float((net > 0).mean()),
        "profit_factor": _profit_factor(net),
        "all_row_prediction_correlation": all_row_correlation,
        "accepted_position_correlation": position_correlation,
        "max_winner_share_of_positive_pnl": concentration,
        "directions": sides,
        "gates": gates,
        "passed": all(gates.values()),
    }


def run(features_root: Path) -> dict:
    samples, evidence_audit = load_samples(features_root)
    predictions, diagnostics = walk_forward_predictions(samples)
    selected, funnel = select_portfolio(predictions)
    monthly = {}
    for month in PREDICTION_MONTHS:
        month_rows = predictions[predictions["model_month"] == month]
        month_selected = selected[selected["model_month"] == month] if not selected.empty else selected
        monthly[month] = summarize_month(month_selected, month_rows)
    passed = all(monthly[month].get("passed", False) for month in PREDICTION_MONTHS)
    ledger = []
    if not selected.empty:
        columns = [
            "decision_time", "exit_time", "symbol", "model_month",
            "evaluation_split", "side", "prediction_pct", TARGET,
            "gross_bps", "net_bps",
        ]
        for row in selected[columns].to_dict("records"):
            ledger.append({
                key: value.isoformat() if isinstance(value, pd.Timestamp) else value
                for key, value in row.items()
            })
    return {
        "method": {
            "strategy_id": "V5_FROZEN_AGGTRADE_MICROSTRUCTURE_V1",
            "authority": "RESEARCH_ONLY",
            "frozen": True,
            "model": {
                "type": "HistGradientBoostingRegressor",
                "max_depth": 3,
                "learning_rate": 0.05,
                "max_iter": 100,
                "l2_regularization": 10.0,
                "random_state": 42,
                "train_only_imputation": "median",
            },
            "target": TARGET,
            "features": list(FEATURES),
            "prediction_months": list(PREDICTION_MONTHS),
            "predicted_move_threshold_pct": MIN_PREDICTED_MOVE_PCT,
            "holding_minutes": HOLD_MINUTES,
            "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "selection": "strongest LONG and strongest SHORT per completed 5m decision, absolute prediction threshold, strongest absolute candidate first under capacity",
        },
        "evidence_audit": evidence_audit,
        "model_diagnostics": diagnostics,
        "portfolio_funnel": funnel,
        "monthly": monthly,
        "selected_ledger": ledger,
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "execution_authority": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features-root", type=Path,
        default=Path("research_data/v5_microstructure_features"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("research_data/v5_microstructure_features/v5_frozen_result.json"),
    )
    args = parser.parse_args()
    result = run(ROOT / args.features_root)
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({
        "verdict": result["verdict"],
        "passed": result["passed"],
        "monthly": result["monthly"],
    }, ensure_ascii=True))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
