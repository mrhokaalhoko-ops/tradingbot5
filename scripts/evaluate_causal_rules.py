"""Evaluate two pre-declared regime rules with symbol episode cooldown."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path

import pandas as pd


RULES = {
    "STRONG_RISK_ON_BUY_10": lambda frame: (
        (frame["global_regime"] == "STRONG_RISK_ON")
        & (frame["return_24_pct"] >= 10.0)
    ),
    "STRONG_RISK_OFF_SELL_10": lambda frame: (
        (frame["global_regime"] == "STRONG_RISK_OFF")
        & (frame["return_24_pct"] <= -10.0)
    ),
}


def _stats(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"events": 0}
    values = frame["net_bps"]
    r_values = frame["net_r_proxy"]
    positives = values[values > 0]
    return {
        "events": len(frame),
        "symbols": int(frame["symbol"].nunique()),
        "positive_rate": float((values > 0).mean()),
        "mean_net_bps": float(values.mean()),
        "median_net_bps": float(values.median()),
        "mean_r_proxy": float(r_values.mean()),
        "median_r_proxy": float(r_values.median()),
        "total_r_proxy": float(r_values.sum()),
        "tp_hit_proxy": int((frame["tp_hit"] & ~frame["sl_hit"]).sum()),
        "sl_hit_proxy": int((frame["sl_hit"] & ~frame["tp_hit"]).sum()),
        "ambiguous_proxy": int((frame["tp_hit"] & frame["sl_hit"]).sum()),
        "neither_proxy": int((~frame["tp_hit"] & ~frame["sl_hit"]).sum()),
        "max_single_positive_share": (
            float(positives.max() / positives.sum()) if len(positives) and positives.sum() > 0 else None
        ),
    }


def _episodes(frame: pd.DataFrame, cooldown_hours: int) -> pd.DataFrame:
    rows = []
    cooldown = timedelta(hours=cooldown_hours)
    for _, group in frame.sort_values(["symbol", "open_time"]).groupby("symbol", sort=False):
        last = None
        for row in group.itertuples(index=False):
            if last is not None and row.open_time < last + cooldown:
                continue
            rows.append(row._asdict())
            last = row.open_time
    return pd.DataFrame(rows, columns=frame.columns)


def _split(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.DataFrame]:
    duration = end - start
    train_end = start + duration * 0.60
    validation_end = start + duration * 0.80
    return {
        "TRAIN": frame[frame.open_time < train_end],
        "VALIDATION": frame[(frame.open_time >= train_end) & (frame.open_time < validation_end)],
        "TEST": frame[frame.open_time >= validation_end],
    }


def evaluate(features_root: Path, cooldown_hours: int = 4) -> dict:
    columns = [
        "open_time", "symbol", "global_regime", "return_24_pct", "atr_pct",
        "future_close_48x5m_from_entry_pct", "future_high_48x5m_from_entry_pct",
        "future_low_48x5m_from_entry_pct", "observation_valid",
    ]
    frames = []
    for path in sorted(features_root.glob("*/*.parquet")):
        frame = pd.read_parquet(path, columns=columns)
        frame = frame[
            frame["observation_valid"]
            & (frame["open_time"].dt.minute == 55)
            & (frame["open_time"].dt.second == 0)
        ].copy()
        frames.append(frame)
    if not frames:
        raise RuntimeError("no feature partitions found")
    source = pd.concat(frames, ignore_index=True)
    start = source["open_time"].min().normalize()
    end = source["open_time"].max().normalize() + pd.Timedelta(days=1)
    output = {}
    for name, selector in RULES.items():
        frame = source[selector(source)].copy()
        side = 1.0 if "BUY" in name else -1.0
        frame["net_bps"] = side * frame["future_close_48x5m_from_entry_pct"] * 100.0 - 12.0
        frame["net_r_proxy"] = (
            (side * frame["future_close_48x5m_from_entry_pct"] - 0.12)
            / frame["atr_pct"].where(frame["atr_pct"] > 0)
        )
        frame["favorable_pct"] = (
            frame["future_high_48x5m_from_entry_pct"]
            if side > 0
            else -frame["future_low_48x5m_from_entry_pct"]
        )
        frame["adverse_pct"] = (
            frame["future_low_48x5m_from_entry_pct"]
            if side > 0
            else -frame["future_high_48x5m_from_entry_pct"]
        )
        frame["tp_hit"] = frame["favorable_pct"] >= (2.0 * frame["atr_pct"])
        frame["sl_hit"] = frame["adverse_pct"] <= -frame["atr_pct"]
        frame = _episodes(frame, cooldown_hours)
        splits = _split(frame, start, end)
        output[name] = {
            "all": _stats(frame),
            "splits": {split: _stats(rows) for split, rows in splits.items()},
            "monthly": {
                month: _stats(rows)
                for month, rows in frame.groupby(frame.open_time.dt.strftime("%Y-%m"))
            },
        }
    return {
        "method": {
            "sample": "closed 5m bar at minute 55 of each hour",
            "entry": "next 5m open",
            "horizon": "4h mark-to-market",
            "costs_bps": 12.0,
            "episode_cooldown_hours": cooldown_hours,
            "rules": list(RULES),
            "note": "Pre-declared descriptive candidates; not an execution recommendation.",
        },
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "rules": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/rule_evaluation.json"))
    parser.add_argument("--cooldown-hours", type=int, default=4)
    args = parser.parse_args()
    result = evaluate(args.features_root, args.cooldown_hours)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({name: data["all"] for name, data in result["rules"].items()}, indent=2))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
