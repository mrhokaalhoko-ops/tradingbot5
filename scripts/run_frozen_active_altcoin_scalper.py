"""Run the frozen active-altcoin event replay; research authority only."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.active_altcoin_scalper import (  # noqa: E402
    ActiveAltcoinConfig,
    enforce_capacity,
    replay_event,
    select_hot_events,
    summarize_records,
)


FEATURE_ROOT = ROOT / "research_data" / "causal_futures_dataset" / "features"
OUTPUT = ROOT / "research_data" / "causal_futures_dataset" / "active_altcoin_scalper_frozen.json"
LEDGER_OUTPUT = ROOT / "research_data" / "causal_futures_dataset" / "active_altcoin_scalper_frozen_ledger.parquet"
EVENT_OUTPUT = ROOT / "research_data" / "causal_futures_dataset" / "active_altcoin_scalper_frozen_events.parquet"
MONTHS = tuple(f"2025-{month:02d}" for month in range(8, 13)) + tuple(
    f"2026-{month:02d}" for month in range(1, 8)
)
SPLITS = {
    "TRAIN": set(MONTHS[:8]),
    "VALIDATION": set(MONTHS[8:10]),
    "TEST": set(MONTHS[10:]),
}
COLUMNS = [
    "open_time", "close_time", "open", "high", "low", "close", "symbol",
    "observation_valid", "return_1_pct", "return_4_pct", "volume_ratio",
    "quote_volume", "body_pct", "close_location", "atr", "global_regime",
]


def load_month(month: str) -> pd.DataFrame:
    frames = [pd.read_parquet(path, columns=COLUMNS) for path in (FEATURE_ROOT / month).glob("*.parquet")]
    if not frames:
        raise FileNotFoundError(f"no causal feature partitions for {month}")
    frame = pd.concat(frames, ignore_index=True)
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
    frame["month"] = month
    return frame.sort_values(["symbol", "open_time"]).reset_index(drop=True)


def passes_gate(summary: dict) -> bool:
    sides = summary.get("sides", {})
    return bool(
        summary.get("positions", 0) >= 100
        and (summary.get("expectancy_r") or 0) > 0
        and (summary.get("profit_factor") or 0) > 1.10
        and (summary.get("max_drawdown_r") is not None and summary["max_drawdown_r"] < 20)
        and all((sides.get(side, {}).get("expectancy_r") or 0) >= 0 for side in ("LONG", "SHORT"))
    )


def run() -> dict:
    config = ActiveAltcoinConfig()
    records = {variant: [] for variant in ("IMMEDIATE_CONTINUATION", "PULLBACK_RECLAIM")}
    event_frames = []
    event_count = 0
    for month in MONTHS:
        frame = load_month(month)
        events = select_hot_events(frame, config)
        event_frames.append(events.assign(month=month))
        event_count += len(events)
        grouped = {symbol: rows.reset_index(drop=True) for symbol, rows in frame.groupby("symbol", sort=False)}
        for _, event in events.iterrows():
            for variant in records:
                row = replay_event(event, grouped[str(event["symbol"])], variant, config)
                row["month"] = month
                records[variant].append(row)
        print(f"{month}: rows={len(frame)} hot_events={len(events)}", flush=True)

    variants = {}
    ledger_rows = []
    for variant, rows in records.items():
        adjusted, rejected_capacity = enforce_capacity(rows, config.max_open_positions)
        ledger_rows.extend(adjusted)
        split_summary = {
            split: summarize_records(row for row in adjusted if row["month"] in months)
            for split, months in SPLITS.items()
        }
        gates = {name: passes_gate(split_summary[name]) for name in ("VALIDATION", "TEST")}
        variants[variant] = {
            "raw_records": len(rows),
            "rejected_capacity": rejected_capacity,
            "splits": split_summary,
            "gates": gates,
            "passed": all(gates.values()),
        }
    passed = any(row["passed"] for row in variants.values())
    ledger = pd.DataFrame(ledger_rows)
    if not ledger.empty:
        ledger.to_parquet(LEDGER_OUTPUT, index=False)
    all_events = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()
    if not all_events.empty:
        all_events.to_parquet(EVENT_OUTPUT, index=False)
    return {
        "method": {
            "strategy_id": "ACTIVE_ALTCOIN_SCALPER_FROZEN_V1",
            "authority": "RESEARCH_ONLY",
            "protocol": "ACTIVE_ALTCOIN_SCALPER_FROZEN_PROTOCOL_2026-09-03.md",
            "months": list(MONTHS),
            "splits": {name: sorted(months) for name, months in SPLITS.items()},
            "config": config.__dict__,
        },
        "hot_events": event_count,
        "ledger": str(LEDGER_OUTPUT),
        "ledger_rows": len(ledger),
        "events": str(EVENT_OUTPUT),
        "variants": variants,
        "passed": passed,
        "verdict": "PASS_FOR_SHADOW_ONLY" if passed else "REJECT",
        "execution_authority": False,
    }


def main() -> int:
    result = run()
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "hot_events": result["hot_events"], "variants": result["variants"]}, ensure_ascii=True))
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
