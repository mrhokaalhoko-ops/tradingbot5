"""Frozen V3 walk-forward cross-sectional ranking experiment.

The model is deliberately linear, retrains from past data only, and has no
execution authority.  Its only purpose is to test whether causal OHLCV/context
features retain directional information after temporal adaptation and costs.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


TARGET = "future_close_48x5m_from_entry_pct"
ROUND_TRIP_COST_BPS = 12.0
MIN_PREDICTED_MOVE_PCT = 0.25
TRAIN_DAYS = 180
EMBARGO_DAYS = 7
HOLD_HOURS = 4
MAX_OPEN_POSITIONS = 5
PREDICTION_MONTHS = (
    "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
)

RAW_FEATURES = [
    "return_4_pct", "return_24_pct",
    "15m_return_1_pct", "15m_return_4_pct", "15m_return_24_pct",
    "1h_return_1_pct", "1h_return_4_pct", "1h_return_24_pct",
    "ema_alignment", "15m_ema_alignment", "1h_ema_alignment",
    "ema20_distance_atr", "15m_ema20_distance_atr", "1h_ema20_distance_atr",
    "atr_pct", "15m_atr_pct", "1h_atr_pct",
    "volume_ratio", "15m_volume_ratio", "1h_volume_ratio",
    "close_location", "body_pct",
    "btc_six_hour_return_pct", "eth_six_hour_return_pct", "selection_rank",
]
RANK_SOURCES = [
    "return_4_pct", "return_24_pct",
    "15m_return_4_pct", "15m_return_24_pct",
    "1h_return_4_pct", "1h_return_24_pct",
    "ema20_distance_atr", "15m_ema20_distance_atr", "1h_ema20_distance_atr",
    "atr_pct", "volume_ratio", "15m_volume_ratio", "1h_volume_ratio",
]
RANK_FEATURES = [f"xrank_{name}" for name in RANK_SOURCES]
NUMERIC_FEATURES = [*RAW_FEATURES, *RANK_FEATURES]
CATEGORICAL_FEATURES = ["global_regime"]


def evaluation_split(timestamp: pd.Timestamp) -> str:
    month = timestamp.strftime("%Y-%m")
    if month in {"2026-02", "2026-03"}:
        return "ADAPTATION_DIAGNOSTIC"
    if month in {"2026-04", "2026-05"}:
        return "VALIDATION"
    if month in {"2026-06", "2026-07"}:
        return "TEST"
    return "OUTSIDE_EVALUATION"


def training_window(month: str) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    month_start = pd.Timestamp(f"{month}-01", tz="UTC")
    prediction_end = month_start + pd.offsets.MonthBegin(1)
    train_end = month_start - pd.Timedelta(days=EMBARGO_DAYS)
    train_start = train_end - pd.Timedelta(days=TRAIN_DAYS)
    return train_start, train_end, prediction_end


def add_cross_sectional_ranks(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    grouped = result.groupby("open_time", sort=False)
    for source in RANK_SOURCES:
        result[f"xrank_{source}"] = grouped[source].rank(
            method="average", pct=True
        )
    return result


def load_samples(features_root: Path, crypto_symbols: set[str]) -> pd.DataFrame:
    columns = [
        "open_time", "close_time", "symbol", "observation_valid", TARGET,
        *RAW_FEATURES, *CATEGORICAL_FEATURES,
    ]
    frames = []
    for path in sorted(features_root.glob("*/*.parquet")):
        if path.stem not in crypto_symbols:
            continue
        frame = pd.read_parquet(path, columns=columns)
        # One decision after every completed hour.  All attached 15m/1h
        # features have already closed at this row's close time.
        selected = frame[
            frame["observation_valid"] & (frame["open_time"].dt.minute == 55)
        ].copy()
        if not selected.empty:
            frames.append(selected)
    if not frames:
        return pd.DataFrame(columns=[*columns, *RANK_FEATURES])
    result = pd.concat(frames, ignore_index=True)
    result[RAW_FEATURES] = result[RAW_FEATURES].replace([np.inf, -np.inf], np.nan)
    result = result[np.isfinite(result[TARGET])].copy()
    result = add_cross_sectional_ranks(result)
    return result.sort_values(["open_time", "symbol"]).reset_index(drop=True)


def build_model(numeric_features: list[str] | None = None) -> Pipeline:
    numeric_features = list(numeric_features or NUMERIC_FEATURES)
    numeric = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
    ])
    preprocess = ColumnTransformer([
        ("numeric", numeric, numeric_features),
        (
            "regime",
            OneHotEncoder(handle_unknown="ignore", sparse_output=False),
            CATEGORICAL_FEATURES,
        ),
    ])
    return Pipeline([
        ("preprocess", preprocess),
        ("model", Ridge(alpha=10.0)),
    ])


def walk_forward_predictions(
    samples: pd.DataFrame,
    numeric_features: list[str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    numeric_features = list(numeric_features or NUMERIC_FEATURES)
    feature_columns = [*numeric_features, *CATEGORICAL_FEATURES]
    predictions = []
    diagnostics = {}
    for month in PREDICTION_MONTHS:
        train_start, train_end, prediction_end = training_window(month)
        month_start = pd.Timestamp(f"{month}-01", tz="UTC")
        train = samples[
            (samples["open_time"] >= train_start)
            & (samples["open_time"] < train_end)
        ].copy()
        future = samples[
            (samples["open_time"] >= month_start)
            & (samples["open_time"] < prediction_end)
        ].copy()
        if train.empty or future.empty:
            diagnostics[month] = {
                "status": "INSUFFICIENT_DATA",
                "train_rows": int(len(train)),
                "prediction_rows": int(len(future)),
            }
            continue
        model = build_model(numeric_features)
        model.fit(train[feature_columns], train[TARGET])
        future["prediction_pct"] = model.predict(future[feature_columns])
        future["evaluation_split"] = future["open_time"].map(evaluation_split)
        future["model_month"] = month
        predictions.append(future)
        diagnostics[month] = {
            "status": "PREDICTED",
            "train_start": train_start.isoformat(),
            "train_end_exclusive": train_end.isoformat(),
            "prediction_start": month_start.isoformat(),
            "prediction_end_exclusive": prediction_end.isoformat(),
            "train_rows": int(len(train)),
            "prediction_rows": int(len(future)),
            "prediction_correlation": float(
                future["prediction_pct"].corr(future[TARGET])
            ),
            "prediction_mae_pct": float(
                (future["prediction_pct"] - future[TARGET]).abs().mean()
            ),
        }
    if not predictions:
        return pd.DataFrame(), diagnostics
    return pd.concat(predictions, ignore_index=True).sort_values(
        ["open_time", "symbol"]
    ), diagnostics


def select_portfolio(predictions: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Select at most one long and one short, then enforce causal capacity."""
    if predictions.empty:
        return predictions.copy(), {}
    active: list[tuple[pd.Timestamp, str]] = []
    accepted = []
    counters: Counter[str] = Counter()
    for timestamp, group in predictions.groupby("open_time", sort=True):
        active = [item for item in active if item[0] > timestamp]
        positive = group[group["prediction_pct"] >= MIN_PREDICTED_MOVE_PCT]
        negative = group[group["prediction_pct"] <= -MIN_PREDICTED_MOVE_PCT]
        candidates = []
        if not positive.empty:
            candidates.append(positive.nlargest(1, "prediction_pct").iloc[0])
        if not negative.empty:
            candidates.append(negative.nsmallest(1, "prediction_pct").iloc[0])
        counters["timestamps"] += 1
        counters["directional_candidates"] += len(candidates)
        for row in sorted(
            candidates, key=lambda item: abs(float(item["prediction_pct"])), reverse=True
        ):
            symbol = str(row["symbol"])
            if any(active_symbol == symbol for _, active_symbol in active):
                counters["rejected_symbol_busy"] += 1
                continue
            if len(active) >= MAX_OPEN_POSITIONS:
                counters["rejected_capacity"] += 1
                continue
            accepted.append(row.to_dict())
            active.append((timestamp + pd.Timedelta(hours=HOLD_HOURS), symbol))
    result = pd.DataFrame(accepted)
    if result.empty:
        return result, dict(counters)
    result["side"] = np.where(result["prediction_pct"] >= 0, "BUY", "SELL")
    direction = np.where(result["side"] == "BUY", 1.0, -1.0)
    result["net_bps"] = direction * result[TARGET] * 100.0 - ROUND_TRIP_COST_BPS
    return result.sort_values(["open_time", "symbol"]), dict(counters)


def summarize(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"trades": 0}
    values = frame["net_bps"].astype(float)
    winners = values[values > 0]
    losers = values[values <= 0]
    gross_profit = float(winners.sum())
    gross_loss = abs(float(losers.sum()))
    monthly = {
        month: {
            "trades": int(len(group)),
            "mean_net_bps": float(group["net_bps"].mean()),
            "median_net_bps": float(group["net_bps"].median()),
            "total_net_bps": float(group["net_bps"].sum()),
        }
        for month, group in frame.groupby(
            frame["open_time"].dt.strftime("%Y-%m"), sort=True
        )
    }
    return {
        "trades": int(len(values)),
        "mean_net_bps": float(values.mean()),
        "median_net_bps": float(values.median()),
        "positive_rate": float((values > 0).mean()),
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (float("inf") if gross_profit > 0 else None)
        ),
        "monthly": monthly,
    }


def assess(selected: pd.DataFrame) -> tuple[dict, bool]:
    reports = {
        split: summarize(selected[selected["evaluation_split"] == split])
        for split in ("ADAPTATION_DIAGNOSTIC", "VALIDATION", "TEST")
    }
    validation = reports["VALIDATION"]
    test = reports["TEST"]
    holdout_months = {
        **validation.get("monthly", {}), **test.get("monthly", {})
    }
    passed = all([
        validation.get("trades", 0) >= 300,
        test.get("trades", 0) >= 300,
        validation.get("mean_net_bps", 0) > 0,
        test.get("mean_net_bps", 0) > 0,
        (validation.get("profit_factor") or 0) > 1.10,
        (test.get("profit_factor") or 0) > 1.10,
        len(holdout_months) == 4,
        all(row["total_net_bps"] > 0 for row in holdout_months.values()),
    ])
    return reports, passed


def run(features_root: Path, metadata_path: Path) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    crypto_symbols = {
        row["symbol"] for row in metadata["symbols"]
        if row["underlying_type"] == "COIN"
    }
    samples = load_samples(features_root, crypto_symbols)
    predictions, diagnostics = walk_forward_predictions(samples)
    selected, capacity = select_portfolio(predictions)
    reports, passed = assess(selected)
    ledger_columns = [
        "open_time", "symbol", "model_month", "evaluation_split",
        "prediction_pct", TARGET, "side", "net_bps",
    ]
    ledger = [
        {
            key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
            for key, value in row.items()
        }
        for row in selected[ledger_columns].to_dict("records")
    ] if not selected.empty else []
    return {
        "method": {
            "strategy_id": "V3_WALK_FORWARD_RIDGE_RANKING_V1",
            "frozen": True,
            "authority": "RESEARCH_ONLY",
            "model": "Ridge(alpha=10) with TRAIN-only median imputation and scaling",
            "features": NUMERIC_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
            "cross_sectional_features": RANK_FEATURES,
            "target": TARGET,
            "decision_clock": "hourly after completed 1h candle",
            "training_window_days": TRAIN_DAYS,
            "embargo_days": EMBARGO_DAYS,
            "prediction_months": PREDICTION_MONTHS,
            "selection": (
                "at most strongest long and strongest short per hour; "
                f"abs prediction >= {MIN_PREDICTED_MOVE_PCT}%"
            ),
            "holding_period_hours": HOLD_HOURS,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
            "warning": "Directional time-exit ranking only; execution replay is gated on pass.",
        },
        "sample_rows": int(len(samples)),
        "prediction_rows": int(len(predictions)),
        "model_diagnostics": diagnostics,
        "portfolio_funnel": capacity,
        "reports": reports,
        "selected_ledger": ledger,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features-root", type=Path,
        default=Path("research_data/causal_futures_dataset/features"),
    )
    parser.add_argument(
        "--metadata", type=Path,
        default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("research_data/causal_futures_dataset/v3_walk_forward_ridge.json"),
    )
    args = parser.parse_args()
    result = run(args.features_root, args.metadata)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(args.output)
    print(json.dumps(result["reports"], ensure_ascii=True))
    print(f"passed={result['passed']} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
