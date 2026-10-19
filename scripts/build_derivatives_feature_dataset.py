"""Build compact causal derivatives-feature partitions beside OHLCV features."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.derivatives_features import (
    FUNDING_FEATURE_COLUMNS,
    METRIC_DERIVED_COLUMNS,
    METRIC_VALUE_COLUMNS,
    add_metrics_features,
    attach_derivatives_features,
    load_funding_frame,
    load_metrics_frame,
)


OUTPUT_COLUMNS = [
    "close_time", "symbol", "target_month",
    "metrics_time", *METRIC_VALUE_COLUMNS, *METRIC_DERIVED_COLUMNS,
    "metrics_staleness_seconds",
    "funding_time", *FUNDING_FEATURE_COLUMNS, "funding_staleness_seconds",
]


def _paths(root: Path, symbol: str, kind: str) -> list[Path]:
    if kind == "metrics":
        return sorted((root / "archives" / "metrics" / symbol).glob("*/*.zip"))
    return sorted((root / "archives" / "funding" / symbol).glob("*.zip"))


def build_symbol(
    root: Path,
    features_root: Path,
    output_root: Path,
    symbol: str,
    months: list[str],
) -> list[dict]:
    metrics = add_metrics_features(load_metrics_frame(_paths(root, symbol, "metrics")))
    funding = load_funding_frame(_paths(root, symbol, "funding"))
    reports = []
    for month in sorted(months):
        source = features_root / month / f"{symbol}.parquet"
        if not source.exists():
            reports.append({"symbol": symbol, "month": month, "status": "MISSING_BASE"})
            continue
        observations = pd.read_parquet(source, columns=["close_time", "symbol", "target_month"])
        joined = attach_derivatives_features(observations, metrics, funding)
        if (
            joined["metrics_time"].notna()
            & (joined["metrics_time"] > joined["close_time"])
        ).any():
            raise ValueError(f"future metrics leakage in {symbol} {month}")
        if (
            joined["funding_time"].notna()
            & (joined["funding_time"] > joined["close_time"])
        ).any():
            raise ValueError(f"future funding leakage in {symbol} {month}")
        output = output_root / month / f"{symbol}.parquet"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".parquet.tmp")
        joined[OUTPUT_COLUMNS].to_parquet(temporary, index=False, compression="zstd")
        temporary.replace(output)
        reports.append({
            "symbol": symbol,
            "month": month,
            "status": "OK",
            "rows": len(joined),
            "metrics_coverage": float(joined["metrics_time"].notna().mean()),
            "funding_coverage": float(joined["funding_time"].notna().mean()),
            "max_metrics_staleness_seconds": (
                float(joined["metrics_staleness_seconds"].max())
                if joined["metrics_staleness_seconds"].notna().any() else None
            ),
            "max_funding_staleness_seconds": (
                float(joined["funding_staleness_seconds"].max())
                if joined["funding_staleness_seconds"].notna().any() else None
            ),
            "bytes": output.stat().st_size,
        })
    return reports


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=Path("research_data/causal_futures_dataset/universe.json"))
    parser.add_argument("--root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--features-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--output-root", type=Path, default=Path("research_data/causal_futures_dataset/derivatives_features"))
    parser.add_argument("--manifest", type=Path, default=Path("research_data/causal_futures_dataset/derivatives_features_manifest.json"))
    args = parser.parse_args()

    universe = json.loads((ROOT / args.universe).read_text(encoding="utf-8"))
    selections: dict[str, list[str]] = {}
    for month, body in universe["monthly"].items():
        for row in body["symbols"]:
            selections.setdefault(str(row["symbol"]), []).append(month)

    root = ROOT / args.root
    features_root = ROOT / args.features_root
    output_root = ROOT / args.output_root
    reports = []
    for completed, (symbol, months) in enumerate(sorted(selections.items()), start=1):
        reports.extend(build_symbol(root, features_root, output_root, symbol, months))
        if completed % 20 == 0:
            print(f"symbols {completed}/{len(selections)}", flush=True)

    status_counts = {}
    for row in reports:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    ok = [row for row in reports if row["status"] == "OK"]
    payload = {
        "dataset": "Causal Binance USD-M OI/position/funding features",
        "partitions": len(reports),
        "status_counts": status_counts,
        "rows": sum(int(row.get("rows", 0)) for row in ok),
        "weighted_metrics_coverage": (
            sum(row["metrics_coverage"] * row["rows"] for row in ok)
            / sum(row["rows"] for row in ok) if ok else None
        ),
        "weighted_funding_coverage": (
            sum(row["funding_coverage"] * row["rows"] for row in ok)
            / sum(row["rows"] for row in ok) if ok else None
        ),
        "reports": reports,
    }
    manifest = ROOT / args.manifest
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest.with_suffix(manifest.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(manifest)
    print(json.dumps({key: payload[key] for key in (
        "partitions", "status_counts", "rows", "weighted_metrics_coverage", "weighted_funding_coverage",
    )}), flush=True)
    print(manifest, flush=True)
    return 0 if status_counts.get("MISSING_BASE", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
