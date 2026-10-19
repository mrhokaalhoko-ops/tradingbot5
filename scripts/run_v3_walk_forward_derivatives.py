"""Frozen incremental V3 comparison with causal OI/funding features."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_frozen_derivatives_ranking import DERIVATIVE_FEATURES
from scripts.run_v3_walk_forward_ridge import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    TARGET,
    assess,
    load_samples,
    select_portfolio,
    walk_forward_predictions,
)


def load_derivative_samples(root: Path, crypto_symbols: set[str]) -> pd.DataFrame:
    columns = ["close_time", "symbol", *DERIVATIVE_FEATURES]
    frames = []
    for path in sorted(root.glob("*/*.parquet")):
        if path.stem not in crypto_symbols:
            continue
        frame = pd.read_parquet(path, columns=columns)
        sampled = frame[frame["close_time"].dt.minute == 59].copy()
        if not sampled.empty:
            frames.append(sampled)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


def merge_derivatives(base: pd.DataFrame, derivatives: pd.DataFrame) -> pd.DataFrame:
    keys = ["close_time", "symbol"]
    if derivatives.duplicated(keys).any():
        raise ValueError("duplicate derivatives timestamp/symbol key")
    result = base.merge(derivatives, how="left", on=keys, validate="one_to_one")
    result[DERIVATIVE_FEATURES] = result[DERIVATIVE_FEATURES].replace(
        [np.inf, -np.inf], np.nan
    )
    return result


def run(features_root: Path, derivatives_root: Path, metadata_path: Path) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    crypto_symbols = {
        row["symbol"] for row in metadata["symbols"]
        if row["underlying_type"] == "COIN"
    }
    base = load_samples(features_root, crypto_symbols)
    derivatives = load_derivative_samples(derivatives_root, crypto_symbols)
    samples = merge_derivatives(base, derivatives)
    numeric_features = [*NUMERIC_FEATURES, *DERIVATIVE_FEATURES]
    predictions, diagnostics = walk_forward_predictions(samples, numeric_features)
    selected, capacity = select_portfolio(predictions)
    reports, passed = assess(selected)
    coverage = {
        month: {
            feature: float(group[feature].notna().mean())
            for feature in DERIVATIVE_FEATURES
        }
        for month, group in samples.groupby(
            samples["open_time"].dt.strftime("%Y-%m"), sort=True
        )
    }
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
            "strategy_id": "V3_WALK_FORWARD_RIDGE_DERIVATIVES_V1",
            "frozen": True,
            "authority": "RESEARCH_ONLY",
            "comparison": "unchanged V3 Ridge policy plus causal OI/funding inputs",
            "base_features": NUMERIC_FEATURES,
            "added_features": DERIVATIVE_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
            "target": TARGET,
            "policy": "identical to V3_WALK_FORWARD_RIDGE_RANKING_V1",
        },
        "sample_rows": int(len(samples)),
        "prediction_rows": int(len(predictions)),
        "derivatives_coverage_by_month": coverage,
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
        "--derivatives-root", type=Path,
        default=Path("research_data/causal_futures_dataset/derivatives_features"),
    )
    parser.add_argument(
        "--metadata", type=Path,
        default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("research_data/causal_futures_dataset/v3_walk_forward_derivatives.json"),
    )
    args = parser.parse_args()
    result = run(args.features_root, args.derivatives_root, args.metadata)
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
