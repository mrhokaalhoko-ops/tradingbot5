"""Frozen V4 market-neutral relative-value walk-forward experiment.

This research-only experiment asks a different question from V3: can the
causal feature set rank the future *spread* between contracts after removing
the common market move?  Every accepted observation is an atomic long/short
pair, so aggregate BTC direction cannot manufacture a directional result.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_v3_walk_forward_ridge import (  # noqa: E402
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    PREDICTION_MONTHS,
    ROUND_TRIP_COST_BPS,
    TARGET,
    build_model,
    evaluation_split,
    load_samples,
    training_window,
)


STRATEGY_ID = "V4_WALK_FORWARD_RELATIVE_VALUE_V1"
RELATIVE_TARGET = "future_cross_sectional_residual_pct"
MIN_PREDICTED_SPREAD_PCT = 0.35
HOLD_HOURS = 4
MAX_OPEN_LEGS = 4
BETA_WINDOW_HOURS = 168
BETA_MIN_HOURS = 48

RELATIVE_FEATURES = [
    "btc_beta_168h",
    "relative_btc_1h_pct",
    "relative_btc_4h_pct",
    "relative_btc_24h_pct",
    "beta_residual_1h_pct",
    "breadth_positive_1h",
    "market_median_1h_pct",
    "market_median_24h_pct",
    "market_dispersion_1h_pct",
    "market_dispersion_24h_pct",
]
V4_NUMERIC_FEATURES = [*NUMERIC_FEATURES, *RELATIVE_FEATURES]


def _rolling_beta(group: pd.DataFrame) -> pd.Series:
    market = group["btc_1h_return_pct"].astype(float)
    asset = group["1h_return_1_pct"].astype(float)
    covariance = asset.rolling(
        BETA_WINDOW_HOURS, min_periods=BETA_MIN_HOURS
    ).cov(market)
    variance = market.rolling(
        BETA_WINDOW_HOURS, min_periods=BETA_MIN_HOURS
    ).var()
    return (covariance / variance.replace(0.0, np.nan)).clip(-3.0, 3.0)


def prepare_relative_samples(samples: pd.DataFrame) -> pd.DataFrame:
    """Attach causal market context and a future label used only for fitting."""
    if samples.empty:
        return samples.copy()
    result = samples.sort_values(["symbol", "open_time"]).copy()
    btc = result[result["symbol"] == "BTCUSDT"][
        [
            "open_time", "1h_return_1_pct", "1h_return_4_pct",
            "1h_return_24_pct",
        ]
    ].rename(columns={
        "1h_return_1_pct": "btc_1h_return_pct",
        "1h_return_4_pct": "btc_4h_return_pct",
        "1h_return_24_pct": "btc_24h_return_pct",
    })
    if btc.empty:
        raise ValueError("BTCUSDT observations are required for V4")
    if btc["open_time"].duplicated().any():
        raise ValueError("duplicate BTCUSDT observation timestamp")
    result = result.merge(btc, how="left", on="open_time", validate="many_to_one")

    same_time = result.groupby("open_time", sort=False)
    result["breadth_positive_1h"] = same_time["1h_return_1_pct"].transform(
        lambda values: (values > 0).mean()
    )
    result["market_median_1h_pct"] = same_time["1h_return_1_pct"].transform("median")
    result["market_median_24h_pct"] = same_time["1h_return_24_pct"].transform("median")
    result["market_dispersion_1h_pct"] = same_time["1h_return_1_pct"].transform("std")
    result["market_dispersion_24h_pct"] = same_time["1h_return_24_pct"].transform("std")
    # The median below is a training/evaluation label only.  It is never a
    # feature and is unavailable to the model at inference time.
    future_median = same_time[TARGET].transform("median")
    result[RELATIVE_TARGET] = result[TARGET] - future_median

    result = result.sort_values(["symbol", "open_time"])
    result["btc_beta_168h"] = result.groupby("symbol", group_keys=False).apply(
        _rolling_beta, include_groups=False
    ).reset_index(level=0, drop=True)
    result["relative_btc_1h_pct"] = (
        result["1h_return_1_pct"] - result["btc_1h_return_pct"]
    )
    result["relative_btc_4h_pct"] = (
        result["1h_return_4_pct"] - result["btc_4h_return_pct"]
    )
    result["relative_btc_24h_pct"] = (
        result["1h_return_24_pct"] - result["btc_24h_return_pct"]
    )
    result["beta_residual_1h_pct"] = (
        result["1h_return_1_pct"]
        - result["btc_beta_168h"] * result["btc_1h_return_pct"]
    )
    result[RELATIVE_FEATURES] = result[RELATIVE_FEATURES].replace(
        [np.inf, -np.inf], np.nan
    )
    return result.sort_values(["open_time", "symbol"]).reset_index(drop=True)


def walk_forward_predictions(samples: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    feature_columns = [*V4_NUMERIC_FEATURES, *CATEGORICAL_FEATURES]
    predictions: list[pd.DataFrame] = []
    diagnostics: dict[str, dict] = {}
    for month in PREDICTION_MONTHS:
        train_start, train_end, prediction_end = training_window(month)
        month_start = pd.Timestamp(f"{month}-01", tz="UTC")
        train = samples[
            (samples["open_time"] >= train_start)
            & (samples["open_time"] < train_end)
            & np.isfinite(samples[RELATIVE_TARGET])
        ].copy()
        future = samples[
            (samples["open_time"] >= month_start)
            & (samples["open_time"] < prediction_end)
            & np.isfinite(samples[RELATIVE_TARGET])
        ].copy()
        if train.empty or future.empty:
            diagnostics[month] = {
                "status": "INSUFFICIENT_DATA",
                "train_rows": int(len(train)),
                "prediction_rows": int(len(future)),
            }
            continue
        model = build_model(V4_NUMERIC_FEATURES)
        model.fit(train[feature_columns], train[RELATIVE_TARGET])
        future["prediction_residual_pct"] = model.predict(future[feature_columns])
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
                future["prediction_residual_pct"].corr(future[RELATIVE_TARGET])
            ),
        }
    if not predictions:
        return pd.DataFrame(), diagnostics
    return pd.concat(predictions, ignore_index=True).sort_values(
        ["open_time", "symbol"]
    ), diagnostics


def select_pairs(predictions: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Select one atomic long/short spread per hour and enforce leg capacity."""
    if predictions.empty:
        return pd.DataFrame(), {}
    active: list[tuple[pd.Timestamp, frozenset[str]]] = []
    accepted: list[dict] = []
    counters: Counter[str] = Counter()
    for timestamp, group in predictions.groupby("open_time", sort=True):
        active = [pair for pair in active if pair[0] > timestamp]
        counters["timestamps"] += 1
        if len(group) < 2:
            counters["rejected_insufficient_cross_section"] += 1
            continue
        long_row = group.nlargest(1, "prediction_residual_pct").iloc[0]
        short_row = group.nsmallest(1, "prediction_residual_pct").iloc[0]
        long_symbol = str(long_row["symbol"])
        short_symbol = str(short_row["symbol"])
        spread = float(
            long_row["prediction_residual_pct"]
            - short_row["prediction_residual_pct"]
        )
        if long_symbol == short_symbol or spread < MIN_PREDICTED_SPREAD_PCT:
            counters["rejected_spread_threshold"] += 1
            continue
        busy_symbols = set().union(*(symbols for _, symbols in active)) if active else set()
        if long_symbol in busy_symbols or short_symbol in busy_symbols:
            counters["rejected_symbol_busy"] += 1
            continue
        if 2 * len(active) + 2 > MAX_OPEN_LEGS:
            counters["rejected_capacity"] += 1
            continue
        long_return = float(long_row[TARGET])
        short_return = float(short_row[TARGET])
        accepted.append({
            "open_time": timestamp,
            "model_month": str(long_row["model_month"]),
            "evaluation_split": str(long_row["evaluation_split"]),
            "long_symbol": long_symbol,
            "short_symbol": short_symbol,
            "predicted_spread_pct": spread,
            "long_return_pct": long_return,
            "short_return_pct": short_return,
            "gross_spread_bps": (long_return - short_return) * 100.0,
            "net_pair_bps": (
                (long_return - short_return) * 100.0
                - 2.0 * ROUND_TRIP_COST_BPS
            ),
        })
        active.append((
            timestamp + pd.Timedelta(hours=HOLD_HOURS),
            frozenset({long_symbol, short_symbol}),
        ))
        counters["accepted_pairs"] += 1
    return pd.DataFrame(accepted), dict(counters)


def summarize(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"pairs": 0, "legs": 0}
    values = frame["net_pair_bps"].astype(float)
    winners = values[values > 0]
    losers = values[values <= 0]
    gross_profit = float(winners.sum())
    gross_loss = abs(float(losers.sum()))
    monthly = {
        month: {
            "pairs": int(len(group)),
            "mean_net_pair_bps": float(group["net_pair_bps"].mean()),
            "total_net_pair_bps": float(group["net_pair_bps"].sum()),
        }
        for month, group in frame.groupby(
            frame["open_time"].dt.strftime("%Y-%m"), sort=True
        )
    }
    return {
        "pairs": int(len(frame)),
        "legs": int(2 * len(frame)),
        "mean_gross_spread_bps": float(frame["gross_spread_bps"].mean()),
        "mean_net_pair_bps": float(values.mean()),
        "median_net_pair_bps": float(values.median()),
        "positive_rate": float((values > 0).mean()),
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (float("inf") if gross_profit > 0 else None)
        ),
        "monthly": monthly,
    }


def assess(pairs: pd.DataFrame) -> tuple[dict, bool]:
    reports = {
        split: summarize(pairs[pairs["evaluation_split"] == split])
        for split in ("ADAPTATION_DIAGNOSTIC", "VALIDATION", "TEST")
    }
    validation = reports["VALIDATION"]
    test = reports["TEST"]
    months = {**validation.get("monthly", {}), **test.get("monthly", {})}
    passed = all([
        validation.get("pairs", 0) >= 150,
        test.get("pairs", 0) >= 150,
        validation.get("mean_net_pair_bps", 0) > 0,
        test.get("mean_net_pair_bps", 0) > 0,
        (validation.get("profit_factor") or 0) > 1.10,
        (test.get("profit_factor") or 0) > 1.10,
        len(months) == 4,
        all(row["total_net_pair_bps"] > 0 for row in months.values()),
    ])
    return reports, passed


def run(features_root: Path, metadata_path: Path) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    crypto_symbols = {
        row["symbol"] for row in metadata["symbols"]
        if row["underlying_type"] == "COIN"
    }
    samples = prepare_relative_samples(load_samples(features_root, crypto_symbols))
    predictions, diagnostics = walk_forward_predictions(samples)
    pairs, funnel = select_pairs(predictions)
    reports, passed = assess(pairs)
    ledger = []
    if not pairs.empty:
        ledger = [
            {
                key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
                for key, value in row.items()
            }
            for row in pairs.to_dict("records")
        ]
    return {
        "method": {
            "strategy_id": STRATEGY_ID,
            "frozen": True,
            "authority": "RESEARCH_ONLY",
            "model": "Ridge(alpha=10), rolling 180d, 7d embargo",
            "target": RELATIVE_TARGET,
            "selection": (
                "atomic highest-vs-lowest predicted residual pair; "
                f"predicted spread >= {MIN_PREDICTED_SPREAD_PCT}%"
            ),
            "features": V4_NUMERIC_FEATURES,
            "holding_period_hours": HOLD_HOURS,
            "max_open_legs": MAX_OPEN_LEGS,
            "cost_per_leg_round_trip_bps": ROUND_TRIP_COST_BPS,
            "prediction_months": PREDICTION_MONTHS,
            "warning": "Directional time-exit pair study; no execution authority.",
        },
        "sample_rows": int(len(samples)),
        "prediction_rows": int(len(predictions)),
        "model_diagnostics": diagnostics,
        "portfolio_funnel": funnel,
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
        default=Path("research_data/causal_futures_dataset/v4_walk_forward_relative_value.json"),
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
