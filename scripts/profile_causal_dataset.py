"""Profile causal event outcomes without fitting or changing any rule."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import pandas as pd


def _bucket(value: float, threshold: float) -> str | None:
    if value >= threshold:
        return "BUY"
    if value <= -threshold:
        return "SELL"
    return None


def _summary(values: list[dict]) -> dict:
    if not values:
        return {"events": 0}
    frame = pd.DataFrame(values)
    net_bps = frame["net_bps"]
    return {
        "events": len(frame),
        "positive_rate": float((net_bps > 0).mean()),
        "mean_net_bps": float(net_bps.mean()),
        "median_net_bps": float(net_bps.median()),
        "mean_mfe_pct": float(frame["mfe_pct"].mean()),
        "mean_mae_pct": float(frame["mae_pct"].mean()),
        "atr_1r_target_2r": {
            "tp_first_proxy": int((frame["tp_hit"] & ~frame["sl_hit"]).sum()),
            "sl_first_proxy": int((frame["sl_hit"] & ~frame["tp_hit"]).sum()),
            "ambiguous": int((frame["tp_hit"] & frame["sl_hit"]).sum()),
            "neither_by_horizon": int((~frame["tp_hit"] & ~frame["sl_hit"]).sum()),
        },
    }


def profile(features_root: Path, thresholds: tuple[float, ...]) -> dict:
    columns = [
        "open_time", "symbol", "global_regime", "observation_valid",
        "return_24_pct", "atr_pct", "future_close_48x5m_from_entry_pct",
        "future_high_48x5m_from_entry_pct", "future_low_48x5m_from_entry_pct",
    ]
    groups: dict[str, list[dict]] = defaultdict(list)
    total_rows = 0
    valid_rows = 0
    sampled_rows = 0
    for path in sorted(features_root.glob("*/*.parquet")):
        frame = pd.read_parquet(path, columns=columns)
        total_rows += len(frame)
        frame = frame[frame["observation_valid"]].copy()
        valid_rows += len(frame)
        # One observation per hour/symbol, aligned to the just-closed 5m bar.
        frame = frame[
            (frame["open_time"].dt.minute == 55)
            & (frame["open_time"].dt.second == 0)
        ]
        sampled_rows += len(frame)
        frame["net_bps"] = frame["future_close_48x5m_from_entry_pct"] * 100.0 - 12.0
        frame["mfe_pct"] = frame["future_high_48x5m_from_entry_pct"]
        frame["mae_pct"] = -frame["future_low_48x5m_from_entry_pct"]
        frame["tp_hit"] = frame["future_high_48x5m_from_entry_pct"] >= (2.0 * frame["atr_pct"])
        frame["sl_hit"] = frame["future_low_48x5m_from_entry_pct"] <= -frame["atr_pct"]
        for threshold in thresholds:
            key_threshold = f"abs24_ge_{threshold:g}pct"
            frame["side"] = frame["return_24_pct"].map(lambda value: _bucket(float(value), threshold))
            selected = frame[frame["side"].notna()]
            for row in selected.itertuples(index=False):
                key = f"{key_threshold}|{row.global_regime}|{row.side}"
                groups[key].append({
                    "net_bps": float(row.net_bps),
                    "mfe_pct": float(row.mfe_pct),
                    "mae_pct": float(row.mae_pct),
                    "tp_hit": bool(row.tp_hit),
                    "sl_hit": bool(row.sl_hit),
                })
    return {
        "method": {
            "sample": "closed 5m bars at minute 55 of every hour",
            "entry": "next 5m open",
            "horizon": "48 subsequent 5m bars (4h)",
            "costs_bps": 12.0,
            "r_proxy": "1 ATR stop / 2 ATR target; same-window touches are ambiguous",
            "note": "descriptive profile only; no threshold was selected from this output",
        },
        "rows": {"total": total_rows, "valid": valid_rows, "sampled": sampled_rows},
        "groups": {key: _summary(rows) for key, rows in sorted(groups.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/profile.json"))
    args = parser.parse_args()
    result = profile(args.features_root, (5.0, 10.0, 15.0))
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result["rows"], indent=2))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
