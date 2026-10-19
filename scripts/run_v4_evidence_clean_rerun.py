"""Evidence-clean rerun of the frozen V4 relative-value specification.

No model, threshold, holding period, cost or acceptance criterion is changed.
This rerun only repairs research provenance and sample construction:

* causal cross-sectional membership is established before forward labels;
* the primary run uses the full prior-month-selected historical universe;
* rolling beta uses an actual 168-hour time window;
* input/code/package fingerprints are embedded in the result.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_v3_walk_forward_ridge import (  # noqa: E402
    CATEGORICAL_FEATURES,
    RAW_FEATURES,
    RANK_FEATURES,
    TARGET,
    add_cross_sectional_ranks,
)
from scripts.run_v4_walk_forward_relative_value import (  # noqa: E402
    BETA_MIN_HOURS,
    BETA_WINDOW_HOURS,
    RELATIVE_FEATURES,
    RELATIVE_TARGET,
    STRATEGY_ID,
    V4_NUMERIC_FEATURES,
    assess,
    select_pairs,
    walk_forward_predictions,
)


PAST_ELIGIBILITY_COLUMNS = [
    "atr", "ema20", "ema50", "prior_high_24", "prior_low_24",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(paths: list[Path], root: Path) -> str:
    """Hash file names, sizes and contents in deterministic order."""
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def load_causal_membership(
    features_root: Path,
    allowed_symbols: set[str] | None = None,
) -> pd.DataFrame:
    """Build same-time ranks before checking whether a future label exists."""
    columns = [
        "open_time", "close_time", "symbol", TARGET,
        *PAST_ELIGIBILITY_COLUMNS, *RAW_FEATURES, *CATEGORICAL_FEATURES,
    ]
    frames: list[pd.DataFrame] = []
    for path in sorted(features_root.glob("*/*.parquet")):
        if allowed_symbols is not None and path.stem not in allowed_symbols:
            continue
        frame = pd.read_parquet(path, columns=columns)
        hourly = frame[frame["open_time"].dt.minute == 55].copy()
        if not hourly.empty:
            frames.append(hourly)
    if not frames:
        return pd.DataFrame(columns=[*columns, *RANK_FEATURES])
    result = pd.concat(frames, ignore_index=True)
    result[RAW_FEATURES] = result[RAW_FEATURES].replace([np.inf, -np.inf], np.nan)
    # This eligibility mask contains only information known at decision time.
    past_valid = result[PAST_ELIGIBILITY_COLUMNS].notna().all(axis=1)
    result = result[past_valid].copy()
    # Do not inspect TARGET until all causal cross-sectional ranks are fixed.
    result = add_cross_sectional_ranks(result)
    return result.sort_values(["open_time", "symbol"]).reset_index(drop=True)


def time_rolling_beta(group: pd.DataFrame) -> pd.Series:
    """Return beta over the last 168 clock hours, not the last 168 rows."""
    ordered = group.sort_values("open_time")
    index = pd.DatetimeIndex(ordered["open_time"])
    asset = pd.Series(ordered["1h_return_1_pct"].to_numpy(float), index=index)
    market = pd.Series(ordered["btc_1h_return_pct"].to_numpy(float), index=index)
    window = f"{BETA_WINDOW_HOURS}h"
    covariance = asset.rolling(window, min_periods=BETA_MIN_HOURS).cov(market)
    variance = market.rolling(window, min_periods=BETA_MIN_HOURS).var()
    beta = (covariance / variance.replace(0.0, np.nan)).clip(-3.0, 3.0)
    return pd.Series(beta.to_numpy(), index=ordered.index)


def prepare_relative_samples_clean(membership: pd.DataFrame) -> pd.DataFrame:
    if membership.empty:
        return membership.copy()
    result = membership.sort_values(["symbol", "open_time"]).copy()
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
    if btc.empty or btc["open_time"].duplicated().any():
        raise ValueError("unique BTCUSDT observations are required")
    result = result.merge(btc, how="left", on="open_time", validate="many_to_one")
    same_time = result.groupby("open_time", sort=False)
    result["breadth_positive_1h"] = same_time["1h_return_1_pct"].transform(
        lambda values: (values > 0).mean()
    )
    result["market_median_1h_pct"] = same_time["1h_return_1_pct"].transform("median")
    result["market_median_24h_pct"] = same_time["1h_return_24_pct"].transform("median")
    result["market_dispersion_1h_pct"] = same_time["1h_return_1_pct"].transform("std")
    result["market_dispersion_24h_pct"] = same_time["1h_return_24_pct"].transform("std")
    # Forward availability influences only labels, after causal membership and
    # features have been constructed.  Pandas median ignores unavailable NaNs.
    result[RELATIVE_TARGET] = result[TARGET] - same_time[TARGET].transform("median")

    result["btc_beta_168h"] = np.nan
    for _, indices in result.groupby("symbol", sort=False).groups.items():
        group = result.loc[indices]
        beta = time_rolling_beta(group)
        result.loc[beta.index, "btc_beta_168h"] = beta.to_numpy()
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


def run_cohort(membership: pd.DataFrame) -> dict:
    samples = prepare_relative_samples_clean(membership)
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
        "membership_rows_before_label_filter": int(len(samples)),
        "rows_with_complete_target": int(np.isfinite(samples[TARGET]).sum()),
        "prediction_rows": int(len(predictions)),
        "model_diagnostics": diagnostics,
        "portfolio_funnel": funnel,
        "reports": reports,
        "selected_ledger": ledger,
        "passed": passed,
    }


def git_state() -> dict:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        ).strip())
        return {"head": head, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"head": None, "dirty": None}


def provenance(features_root: Path, manifests: list[Path]) -> dict:
    code_paths = [
        Path(__file__).resolve(),
        ROOT / "scripts/run_v4_walk_forward_relative_value.py",
        ROOT / "scripts/run_v3_walk_forward_ridge.py",
    ]
    feature_paths = list(features_root.glob("*/*.parquet"))
    return {
        "git": git_state(),
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "pandas", "pyarrow", "scikit-learn")
        },
        "code_sha256": {
            path.relative_to(ROOT).as_posix(): sha256_file(path)
            for path in code_paths
        },
        "manifest_sha256": {
            path.relative_to(ROOT).as_posix(): sha256_file(path)
            for path in manifests if path.exists()
        },
        "feature_partition_count": len(feature_paths),
        "feature_tree_sha256": sha256_tree(feature_paths, features_root),
    }


def run(features_root: Path, metadata_path: Path, manifests: list[Path]) -> dict:
    all_membership = load_causal_membership(features_root)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    current_crypto = {
        row["symbol"] for row in metadata["symbols"]
        if row["underlying_type"] == "COIN"
    }
    full_symbols = set(all_membership["symbol"].unique())
    unknown = {
        row["symbol"] for row in metadata["symbols"]
        if not row.get("found_in_snapshot") or row["underlying_type"] == "UNKNOWN"
    }
    return {
        "method": {
            "strategy_id": f"{STRATEGY_ID}_EVIDENCE_CLEAN_RERUN",
            "frozen_strategy_spec": True,
            "authority": "RESEARCH_ONLY",
            "changes": [
                "ranks/context built before forward-label availability filter",
                "primary cohort is full prior-month-selected historical universe",
                "beta is an actual 168-clock-hour rolling window",
                "code/input/package hashes embedded",
            ],
            "unchanged": [
                "Ridge alpha", "features", "0.35% predicted spread threshold",
                "4h hold", "4-leg capacity", "12 bps round-trip cost per leg",
                "training windows", "embargo", "acceptance criteria",
            ],
            "primary_scope": "ALL_PRIOR_MONTH_SELECTED_USDT_CONTRACTS",
            "sensitivity_scope": "CURRENT_SNAPSHOT_COIN_CLASSIFICATION",
        },
        "universe_audit": {
            "full_symbols": len(full_symbols),
            "current_snapshot_coin_symbols": len(full_symbols & current_crypto),
            "snapshot_unknown_symbols": sorted(full_symbols & unknown),
            "note": (
                "The sensitivity cohort retains the original post-period type "
                "classification only to quantify its impact; it is not the primary result."
            ),
        },
        "primary_all_historical": run_cohort(all_membership),
        "sensitivity_current_crypto": run_cohort(
            all_membership[all_membership["symbol"].isin(current_crypto)].copy()
        ),
        "provenance": provenance(features_root, manifests),
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
        default=Path(
            "research_data/causal_futures_dataset/"
            "v4_evidence_clean_rerun.json"
        ),
    )
    args = parser.parse_args()
    features_root = (ROOT / args.features_root).resolve()
    metadata_path = (ROOT / args.metadata).resolve()
    manifests = [
        ROOT / "research_data/causal_futures_dataset/universe.json",
        ROOT / "research_data/causal_futures_dataset/features_manifest.json",
        ROOT / "research_data/causal_futures_dataset/klines_manifest.json",
        ROOT / "research_data/causal_futures_dataset/validation.json",
    ]
    result = run(features_root, metadata_path, manifests)
    output = (ROOT / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)
    print(json.dumps({
        "primary": result["primary_all_historical"]["reports"],
        "sensitivity": result["sensitivity_current_crypto"]["reports"],
    }, ensure_ascii=True))
    print(
        "passed_primary="
        f"{result['primary_all_historical']['passed']} output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
