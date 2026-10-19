"""Build causal V5 five-minute decision features from 1m aggTrade buckets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


LABELS = {
    1: "future_return_5m_pct",
    3: "future_return_15m_pct",
    6: "future_return_30m_pct",
    12: "future_return_60m_pct",
}
RANK_SOURCES = (
    "imbalance_5m",
    "imbalance_15m",
    "flow_z_6h",
    "volume_z_6h",
    "return_5m_bps",
    "return_15m_bps",
    "absorption_score",
    "continuation_score",
    "largest_trade_share_5m",
    "total_notional_5m",
)


def _rolling_past_zscore(values: pd.Series, window: int = 72) -> pd.Series:
    past = values.shift(1)
    mean = past.rolling(window, min_periods=24).mean()
    deviation = past.rolling(window, min_periods=24).std()
    return (values - mean) / deviation.replace(0.0, np.nan)


def engineer_symbol_minutes(minutes: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if minutes.empty:
        return pd.DataFrame()
    frame = minutes.copy()
    frame["bucket"] = pd.to_datetime(frame["bucket"], utc=True)
    frame = frame.sort_values("bucket").drop_duplicates("bucket", keep="last")
    frame["block_start"] = frame["bucket"].dt.floor("5min")
    grouped = frame.groupby("block_start", sort=True)
    bars = grouped.agg(
        first_price=("first_price", "first"),
        high_price=("high_price", "max"),
        low_price=("low_price", "min"),
        last_price=("last_price", "last"),
        minute_rows=("bucket", "size"),
        aggregate_trades=("aggregate_trades", "sum"),
        total_notional_5m=("total_notional", "sum"),
        buy_notional_5m=("buy_notional", "sum"),
        sell_notional_5m=("sell_notional", "sum"),
        signed_notional_5m=("signed_notional", "sum"),
        max_trade_notional_5m=("max_trade_notional", "max"),
        last_1m_imbalance=("aggressor_imbalance", "last"),
    ).reset_index()
    bars = bars[bars["minute_rows"] == 5].copy()
    if bars.empty:
        return bars
    bars["decision_time"] = bars["block_start"] + pd.Timedelta(minutes=5)
    bars["symbol"] = symbol
    gap = bars["block_start"].diff().gt(pd.Timedelta(minutes=5))
    bars["segment_id"] = gap.cumsum()
    pieces = []
    for _, segment in bars.groupby("segment_id", sort=False):
        segment = segment.copy()
        total = segment["total_notional_5m"].replace(0.0, np.nan)
        segment["imbalance_5m"] = segment["signed_notional_5m"] / total
        segment["largest_trade_share_5m"] = (
            segment["max_trade_notional_5m"] / total
        )
        segment["return_5m_bps"] = (
            segment["last_price"] / segment["first_price"] - 1.0
        ) * 10_000.0
        signed_15m = segment["signed_notional_5m"].rolling(3, min_periods=3).sum()
        total_15m = segment["total_notional_5m"].rolling(3, min_periods=3).sum()
        segment["imbalance_15m"] = signed_15m / total_15m.replace(0.0, np.nan)
        segment["return_15m_bps"] = (
            segment["last_price"] / segment["first_price"].shift(2) - 1.0
        ) * 10_000.0
        segment["volume_z_6h"] = _rolling_past_zscore(
            segment["total_notional_5m"]
        )
        segment["flow_z_6h"] = _rolling_past_zscore(
            segment["signed_notional_5m"]
        )
        flow_direction = np.sign(segment["signed_notional_5m"])
        segment["flow_price_agreement_bps"] = (
            flow_direction * segment["return_5m_bps"]
        )
        segment["continuation_score"] = (
            segment["flow_z_6h"].abs()
            * segment["flow_price_agreement_bps"].clip(lower=0.0)
        )
        segment["absorption_score"] = (
            segment["flow_z_6h"].abs()
            * (-segment["flow_price_agreement_bps"]).clip(lower=0.0)
        )
        scaled_flow = segment["signed_notional_5m"].abs() / 1_000_000.0
        segment["price_response_bps_per_million"] = (
            segment["return_5m_bps"] / scaled_flow.replace(0.0, np.nan)
        )
        entry = segment["first_price"].shift(-1)
        segment["future_entry_price"] = entry
        for bars_ahead, label in LABELS.items():
            segment[label] = (
                segment["last_price"].shift(-bars_ahead) / entry - 1.0
            ) * 100.0
        pieces.append(segment)
    return pd.concat(pieces, ignore_index=True).sort_values("decision_time")


def add_cross_sectional_ranks(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    grouped = result.groupby("decision_time", sort=False)
    for source in RANK_SOURCES:
        result[f"xrank_{source}"] = grouped[source].rank(
            method="average", pct=True
        )
    # Availability is recorded only after all causal same-time features/ranks.
    result["feature_ready"] = result[
        ["imbalance_15m", "return_15m_bps", "volume_z_6h", "flow_z_6h"]
    ].notna().all(axis=1)
    result["primary_label_available"] = np.isfinite(
        result[LABELS[3]].astype(float)
    )
    return result


def load_symbol_minutes(paths: list[Path]) -> pd.DataFrame:
    frames = [pd.read_parquet(path) for path in sorted(paths)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run(features_root: Path, output_root: Path, source_manifest: Path) -> dict:
    paths_by_symbol: dict[str, list[Path]] = {}
    for path in features_root.glob("*/*.parquet"):
        paths_by_symbol.setdefault(path.stem, []).append(path)
    frames = []
    symbol_reports = []
    for symbol, paths in sorted(paths_by_symbol.items()):
        engineered = engineer_symbol_minutes(load_symbol_minutes(paths), symbol)
        if not engineered.empty:
            frames.append(engineered)
        symbol_reports.append({
            "symbol": symbol,
            "source_partitions": len(paths),
            "decision_rows": int(len(engineered)),
        })
    if not frames:
        raise RuntimeError("no V5 1m feature partitions found")
    combined = add_cross_sectional_ranks(pd.concat(frames, ignore_index=True))
    combined["month"] = combined["decision_time"].dt.strftime("%Y-%m")
    partitions = []
    for month, monthly in combined.groupby("month", sort=True):
        destination = output_root / f"v5_microstructure_{month}.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".parquet.tmp")
        monthly.drop(columns=["month"]).to_parquet(
            temporary, index=False, compression="zstd"
        )
        temporary.replace(destination)
        partitions.append({
            "month": month,
            "rows": int(len(monthly)),
            "feature_ready_rows": int(monthly["feature_ready"].sum()),
            "primary_label_rows": int(monthly["primary_label_available"].sum()),
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
        })
    return {
        "dataset": "V5 causal microstructure decision features",
        "authority": "RESEARCH_ONLY",
        "source_manifest": str(source_manifest),
        "source_manifest_sha256": sha256_file(source_manifest),
        "decision_clock": "after each completed 5m aggregate-trade block",
        "entry_reference": "first aggregate-trade price in next 5m block",
        "primary_label": LABELS[3],
        "rank_sources": list(RANK_SOURCES),
        "symbols": symbol_reports,
        "partitions": partitions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features-root", type=Path,
        default=Path("research_data/v5_aggtrade_dataset/features_1m"),
    )
    parser.add_argument(
        "--output-root", type=Path,
        default=Path("research_data/v5_microstructure_features"),
    )
    parser.add_argument(
        "--source-manifest", type=Path,
        default=Path("research_data/v5_aggtrade_dataset/manifest.json"),
    )
    parser.add_argument(
        "--manifest", type=Path,
        default=Path("research_data/v5_microstructure_features/manifest.json"),
    )
    args = parser.parse_args()
    result = run(
        ROOT / args.features_root,
        ROOT / args.output_root,
        ROOT / args.source_manifest,
    )
    destination = ROOT / args.manifest
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(destination)
    print(json.dumps({
        "symbols": len(result["symbols"]),
        "partitions": len(result["partitions"]),
        "rows": sum(row["rows"] for row in result["partitions"]),
    }))
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
