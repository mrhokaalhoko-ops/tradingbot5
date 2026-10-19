"""Frozen incremental ranking test: OHLCV baseline plus OI/Funding only."""

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

from scripts.run_frozen_causal_ranking_baseline import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    TARGET,
    fit_and_report,
    load_samples,
)


DERIVATIVE_FEATURES = [
    "oi_value_change_1h_pct",
    "oi_value_change_4h_pct",
    "oi_units_change_1h_pct",
    "oi_value_zscore_24h",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
    "funding_rate_bps",
    "funding_rate_24h_bps",
]


def load_derivative_samples(root: Path, crypto_symbols: set[str]) -> pd.DataFrame:
    columns = ["close_time", "symbol", *DERIVATIVE_FEATURES]
    frames = []
    for path in sorted(root.glob("*/*.parquet")):
        if path.stem not in crypto_symbols:
            continue
        frame = pd.read_parquet(path, columns=columns)
        sampled = frame[
            (frame["close_time"].dt.minute == 59)
            & (frame["close_time"].dt.hour.mod(4) == 3)
        ]
        if not sampled.empty:
            frames.append(sampled)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


def merge_derivative_samples(base: pd.DataFrame, derivatives: pd.DataFrame) -> pd.DataFrame:
    keys = ["close_time", "symbol"]
    if derivatives.duplicated(keys).any():
        raise ValueError("duplicate derivatives sample key")
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
    samples = merge_derivative_samples(base, derivatives)
    numeric_features = [*NUMERIC_FEATURES, *DERIVATIVE_FEATURES]
    reports, ledger, passed = fit_and_report(samples, numeric_features)
    coverage = {
        split: {
            feature: float(group[feature].notna().mean())
            for feature in DERIVATIVE_FEATURES
        }
        for split, group in samples.groupby("split", sort=False)
    }
    return {
        "method": {
            "frozen": True,
            "comparison": "OHLCV baseline plus causal Binance OI/position/funding features",
            "base_numeric_features": NUMERIC_FEATURES,
            "added_derivatives_features": DERIVATIVE_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
            "target": TARGET,
            "policy_and_model": "identical to frozen OHLCV baseline",
        },
        "sample_rows": len(samples),
        "derivatives_coverage": coverage,
        "reports": reports,
        "selected_ledger": ledger,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--derivatives-root", type=Path, default=Path("research_data/causal_futures_dataset/derivatives_features"))
    parser.add_argument("--metadata", type=Path, default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/frozen_derivatives_ranking.json"))
    args = parser.parse_args()
    result = run(args.features_root, args.derivatives_root, args.metadata)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({
        name: row["selected"] for name, row in result["reports"].items()
    }, ensure_ascii=True))
    print(f"passed={result['passed']} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
