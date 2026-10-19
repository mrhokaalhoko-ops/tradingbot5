"""Validate coverage, ordering, labels and causal joins of feature partitions."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import expected_bars_in_month


REGIMES = {
    "STRONG_RISK_ON",
    "MILD_RISK_ON",
    "RANGE",
    "MILD_RISK_OFF",
    "STRONG_RISK_OFF",
    "HIGH_VOLATILITY_EVENT",
    "UNKNOWN",
}


def validate_partition(path: Path, expected_month: str) -> dict:
    frame = pd.read_parquet(path)
    errors: list[str] = []
    required = {
        "open_time", "close_time", "open", "high", "low", "close",
        "volume", "quote_volume", "global_regime", "observation_valid",
        "future_close_48x5m_pct", "future_high_48x5m_pct", "future_low_48x5m_pct",
        "future_entry_open_1x5m", "future_close_48x5m_from_entry_pct",
        "future_high_48x5m_from_entry_pct", "future_low_48x5m_from_entry_pct",
    }
    missing = required.difference(frame.columns)
    if missing:
        errors.append(f"missing columns: {sorted(missing)}")
    if not frame["open_time"].is_monotonic_increasing:
        errors.append("open_time is not sorted")
    if frame["open_time"].duplicated().any():
        errors.append("duplicate open_time")
    actual_months = set(frame["open_time"].dt.strftime("%Y-%m"))
    if actual_months != {expected_month}:
        errors.append(f"unexpected target months: {sorted(actual_months)}")
    gaps = int((frame["open_time"].diff().dropna() != pd.Timedelta(minutes=5)).sum())
    if gaps:
        errors.append(f"{gaps} interval gaps within partition")
    expected = expected_bars_in_month(expected_month, 5)
    if len(frame) != expected:
        errors.append(f"rows {len(frame)} != expected {expected}")
    invalid_regimes = set(frame["global_regime"].dropna().astype(str)) - REGIMES
    if invalid_regimes:
        errors.append(f"invalid regimes: {sorted(invalid_regimes)}")
    if not frame["selection_rank"].between(1, 50).all():
        errors.append("selection rank outside 1..50")
    valid = frame[frame["observation_valid"]]
    label_columns = [
        "future_close_1x5m_pct", "future_close_3x5m_pct", "future_close_12x5m_pct",
        "future_close_48x5m_pct", "future_high_48x5m_pct", "future_low_48x5m_pct",
        "future_entry_open_1x5m", "future_close_48x5m_from_entry_pct",
        "future_high_48x5m_from_entry_pct", "future_low_48x5m_from_entry_pct",
    ]
    if not valid[label_columns].notna().all().all():
        errors.append("valid row contains missing forward label")
    return {
        "path": str(path),
        "rows": len(frame),
        "valid_rows": len(valid),
        "gaps": gaps,
        "errors": errors,
    }


def validate_label_alignment(root: Path, paths: list[Path]) -> dict:
    """Check one-step future close labels across each symbol's partitions."""
    errors: list[str] = []
    checked = 0
    by_symbol: dict[str, list[Path]] = {}
    for path in paths:
        by_symbol.setdefault(path.stem, []).append(path)
    for symbol, symbol_paths in by_symbol.items():
        frame = pd.concat(
            [pd.read_parquet(path, columns=["open_time", "close", "future_close_1x5m_pct"]) for path in symbol_paths],
            ignore_index=True,
        ).sort_values("open_time")
        expected = frame["close"].shift(-1).div(frame["close"]).sub(1.0) * 100.0
        contiguous = frame["open_time"].shift(-1).sub(frame["open_time"]) == pd.Timedelta(minutes=5)
        mask = frame["future_close_1x5m_pct"].notna() & expected.notna() & contiguous
        if not (frame.loc[mask, "future_close_1x5m_pct"] - expected[mask]).abs().lt(1e-8).all():
            errors.append(f"future close label mismatch for {symbol}")
        checked += int(mask.sum())
    return {"checked_one_step_labels": checked, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--manifest", type=Path, default=Path("research_data/causal_futures_dataset/features_manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/validation.json"))
    args = parser.parse_args()
    root = ROOT / args.features_root
    paths = sorted(root.glob("*/*.parquet"))
    results = []
    for path in paths:
        results.append(validate_partition(path, path.parent.name))
    label_result = validate_label_alignment(root, paths)
    errors = [error for result in results for error in result["errors"]] + label_result["errors"]
    payload = {
        "partitions": len(paths),
        "rows": sum(result["rows"] for result in results),
        "valid_rows": sum(result["valid_rows"] for result in results),
        "regime_counts": Counter(
            value
            for path in paths
            for value in pd.read_parquet(path, columns=["global_regime"])["global_regime"].dropna().astype(str)
        ),
        "errors": errors,
        "label_alignment": label_result,
    }
    payload["passed"] = not errors
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=int), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=int))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
