"""Frozen walk-forward directional ranking baseline for the causal dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder


NUMERIC_FEATURES = [
    "return_1_pct", "return_4_pct", "return_24_pct",
    "15m_return_1_pct", "15m_return_4_pct", "15m_return_24_pct",
    "1h_return_1_pct", "1h_return_4_pct", "1h_return_24_pct",
    "ema_alignment", "15m_ema_alignment", "1h_ema_alignment",
    "ema20_distance_atr", "15m_ema20_distance_atr", "1h_ema20_distance_atr",
    "atr_pct", "15m_atr_pct", "1h_atr_pct",
    "volume_ratio", "15m_volume_ratio", "1h_volume_ratio",
    "breakout_up_24", "breakdown_down_24", "wick_sweep_high_24", "wick_sweep_low_24",
    "close_location", "body_pct", "btc_six_hour_return_pct", "eth_six_hour_return_pct",
    "selection_rank",
]
CATEGORICAL_FEATURES = ["global_regime"]
TARGET = "future_close_48x5m_from_entry_pct"
ROUND_TRIP_COST_BPS = 12.0
MIN_PREDICTED_MOVE_PCT = 0.20
TOP_PER_TIMESTAMP = 3


def split_name(timestamp: pd.Timestamp) -> str | None:
    month = timestamp.strftime("%Y-%m")
    if "2025-08" <= month <= "2026-03":
        return "TRAIN"
    if "2026-04" <= month <= "2026-05":
        return "VALIDATION"
    if "2026-06" <= month <= "2026-07":
        return "TEST"
    return None


def sanitize_samples(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result[NUMERIC_FEATURES] = result[NUMERIC_FEATURES].replace(
        [np.inf, -np.inf], np.nan
    )
    return result[np.isfinite(result[TARGET])].copy()


def load_samples(features_root: Path, crypto_symbols: set[str]) -> pd.DataFrame:
    columns = [
        "open_time", "close_time", "symbol", "observation_valid", TARGET,
        *NUMERIC_FEATURES, *CATEGORICAL_FEATURES,
    ]
    frames = []
    for path in sorted(features_root.glob("*/*.parquet")):
        if path.stem not in crypto_symbols:
            continue
        frame = pd.read_parquet(path, columns=columns)
        decision_rows = frame[
            frame["observation_valid"]
            & (frame["open_time"].dt.minute == 55)
            & (frame["open_time"].dt.hour.mod(4) == 3)
        ].copy()
        if not decision_rows.empty:
            frames.append(decision_rows)
    if not frames:
        return pd.DataFrame(columns=columns)
    result = pd.concat(frames, ignore_index=True)
    # Zero historical ATR/volume bases can yield +/-inf.  Treat those as
    # missing and let the TRAIN-fitted imputer handle them; never clip them
    # using information from validation or test.
    result = sanitize_samples(result)
    result["split"] = result["open_time"].map(split_name)
    return result[result["split"].notna()].sort_values(["open_time", "symbol"])


def build_model(numeric_features: list[str] | None = None) -> Pipeline:
    numeric_features = list(numeric_features or NUMERIC_FEATURES)
    preprocess = ColumnTransformer([
        ("numeric", SimpleImputer(strategy="median"), numeric_features),
        (
            "regime",
            OneHotEncoder(handle_unknown="ignore", sparse_output=False),
            CATEGORICAL_FEATURES,
        ),
    ])
    model = HistGradientBoostingRegressor(
        learning_rate=0.05,
        max_iter=150,
        max_leaf_nodes=15,
        min_samples_leaf=50,
        l2_regularization=1.0,
        max_bins=64,
        early_stopping=False,
        random_state=20260830,
    )
    return Pipeline([("preprocess", preprocess), ("model", model)])


def select_ranked(frame: pd.DataFrame) -> pd.DataFrame:
    eligible = frame[frame["prediction_pct"].abs() >= MIN_PREDICTED_MOVE_PCT].copy()
    if eligible.empty:
        eligible["side"] = pd.Series(dtype="object")
        eligible["net_bps"] = pd.Series(dtype="float64")
        return eligible
    eligible["abs_prediction"] = eligible["prediction_pct"].abs()
    chosen = (
        eligible.sort_values(
            ["open_time", "abs_prediction", "symbol"],
            ascending=[True, False, True],
        )
        .groupby("open_time", sort=True, as_index=False)
        .head(TOP_PER_TIMESTAMP)
        .copy()
    )
    chosen["side"] = np.where(chosen["prediction_pct"] >= 0, "BUY", "SELL")
    direction = np.where(chosen["side"] == "BUY", 1.0, -1.0)
    chosen["net_bps"] = direction * chosen[TARGET] * 100.0 - ROUND_TRIP_COST_BPS
    return chosen.sort_values(["open_time", "symbol"])


def summarize_selected(frame: pd.DataFrame) -> dict:
    values = frame["net_bps"].dropna().astype(float)
    winners = values[values > 0]
    losers = values[values <= 0]
    gross_profit = float(winners.sum())
    gross_loss = abs(float(losers.sum()))
    by_month = {
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
        "mean_net_bps": float(values.mean()) if len(values) else None,
        "median_net_bps": float(values.median()) if len(values) else None,
        "positive_rate": float((values > 0).mean()) if len(values) else None,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (float("inf") if gross_profit > 0 else None)
        ),
        "by_month": by_month,
    }


def fit_and_report(samples: pd.DataFrame, numeric_features: list[str]) -> tuple[dict, list[dict], bool]:
    train = samples[samples["split"] == "TRAIN"].copy()
    model = build_model(numeric_features)
    feature_columns = numeric_features + CATEGORICAL_FEATURES
    model.fit(train[feature_columns], train[TARGET])

    reports = {}
    ledger = []
    for name in ("TRAIN", "VALIDATION", "TEST"):
        split = samples[samples["split"] == name].copy()
        split["prediction_pct"] = model.predict(split[feature_columns])
        selected = select_ranked(split)
        reports[name] = {
            "observations": int(len(split)),
            "prediction_mae_pct": float(
                np.mean(np.abs(split["prediction_pct"] - split[TARGET]))
            ),
            "prediction_correlation": float(split["prediction_pct"].corr(split[TARGET])),
            "selected": summarize_selected(selected),
        }
        ledger.extend({
            "split": name,
            "open_time": row.open_time.isoformat(),
            "symbol": row.symbol,
            "prediction_pct": float(row.prediction_pct),
            "actual_4h_pct": float(getattr(row, TARGET)),
            "side": row.side,
            "net_bps": float(row.net_bps),
        } for row in selected.itertuples(index=False))

    validation = reports["VALIDATION"]["selected"]
    test = reports["TEST"]["selected"]
    holdout_months = {**validation["by_month"], **test["by_month"]}
    passed = all([
        validation["trades"] >= 300,
        test["trades"] >= 300,
        validation["mean_net_bps"] is not None and validation["mean_net_bps"] > 0,
        test["mean_net_bps"] is not None and test["mean_net_bps"] > 0,
        validation["profit_factor"] is not None and validation["profit_factor"] > 1.10,
        test["profit_factor"] is not None and test["profit_factor"] > 1.10,
        sum(row["total_net_bps"] > 0 for row in holdout_months.values()) >= 3,
    ])
    return reports, ledger, passed


def run(features_root: Path, metadata_path: Path) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    crypto_symbols = {
        row["symbol"] for row in metadata["symbols"]
        if row["underlying_type"] == "COIN"
    }
    samples = load_samples(features_root, crypto_symbols)
    reports, ledger, passed = fit_and_report(samples, NUMERIC_FEATURES)
    return {
        "method": {
            "frozen": True,
            "model": "HistGradientBoostingRegressor",
            "numeric_features": NUMERIC_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
            "target": TARGET,
            "sample": "one post-close observation per symbol every four hours",
            "policy": (
                f"top {TOP_PER_TIMESTAMP} absolute predictions "
                f">= {MIN_PREDICTED_MOVE_PCT}%"
            ),
            "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
            "warning": "Directional ranking baseline only; no stop/target execution semantics.",
        },
        "sample_rows": int(len(samples)),
        "reports": reports,
        "selected_ledger": ledger,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features-root",
        type=Path,
        default=Path("research_data/causal_futures_dataset/features"),
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research_data/causal_futures_dataset/frozen_causal_ranking_baseline.json"),
    )
    args = parser.parse_args()
    result = run(args.features_root, args.metadata)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({
        name: row["selected"]
        for name, row in result["reports"].items()
    }, ensure_ascii=True))
    print(f"passed={result['passed']} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
