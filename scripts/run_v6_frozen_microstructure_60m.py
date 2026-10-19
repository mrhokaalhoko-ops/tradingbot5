"""Run the pre-registered V6 60-minute microstructure ranking experiment."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_v5_frozen_microstructure import (  # noqa: E402
    FEATURES,
    build_model,
    load_samples,
    training_bounds,
)


TARGET = "future_return_60m_pct"
MONTHS = ("2026-06", "2026-07")
THRESHOLD_PCT = 0.25
COST_BPS = 12.0
HOLD_MINUTES = 60
MAX_OPEN = 5


def _profit_factor(values: pd.Series) -> float | None:
    gains = float(values[values > 0].sum())
    losses = abs(float(values[values <= 0].sum()))
    return gains / losses if losses else (float("inf") if gains else None)


def predict(samples: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    predictions = []
    diagnostics = {}
    for month in MONTHS:
        train_start, train_end = training_bounds(month)
        month_start = pd.Timestamp(f"{month}-01T00:00:00Z")
        month_end = month_start + pd.offsets.MonthBegin(1)
        train = samples[
            samples["decision_time"].between(train_start, train_end, inclusive="left")
            & samples["feature_ready"]
            & samples[TARGET].notna()
        ].copy()
        future = samples[
            samples["decision_time"].between(month_start, month_end, inclusive="left")
            & samples["feature_ready"]
            & samples[TARGET].notna()
            & samples["decision_time"].dt.minute.eq(0)
        ].copy()
        model = build_model()
        model.fit(train[FEATURES], train[TARGET])
        future["prediction_pct"] = model.predict(future[FEATURES])
        future["model_month"] = month
        predictions.append(future)
        diagnostics[month] = {
            "train_rows": int(len(train)),
            "prediction_rows": int(len(future)),
            "all_row_correlation": float(future["prediction_pct"].corr(future[TARGET])),
        }
    return pd.concat(predictions, ignore_index=True), diagnostics


def select(predictions: pd.DataFrame) -> pd.DataFrame:
    active: list[tuple[pd.Timestamp, str]] = []
    rows = []
    for timestamp, group in predictions.groupby("decision_time", sort=True):
        active = [item for item in active if item[0] > timestamp]
        candidates = []
        longs = group[group["prediction_pct"].ge(THRESHOLD_PCT)]
        shorts = group[group["prediction_pct"].le(-THRESHOLD_PCT)]
        if not longs.empty:
            candidates.append(longs.nlargest(1, "prediction_pct").iloc[0])
        if not shorts.empty:
            candidates.append(shorts.nsmallest(1, "prediction_pct").iloc[0])
        for row in sorted(candidates, key=lambda item: abs(float(item["prediction_pct"])), reverse=True):
            symbol = str(row["symbol"])
            if len(active) >= MAX_OPEN or any(active_symbol == symbol for _, active_symbol in active):
                continue
            payload = row.to_dict()
            side = "LONG" if float(row["prediction_pct"]) > 0 else "SHORT"
            direction = 1.0 if side == "LONG" else -1.0
            payload.update(
                side=side,
                gross_bps=direction * float(row[TARGET]) * 100.0,
            )
            payload["net_bps"] = payload["gross_bps"] - COST_BPS
            rows.append(payload)
            active.append((timestamp + pd.Timedelta(minutes=HOLD_MINUTES), symbol))
    return pd.DataFrame(rows)


def summarize(frame: pd.DataFrame, predictions: pd.DataFrame) -> dict:
    result = {}
    for month in MONTHS:
        rows = frame[frame["model_month"].eq(month)] if not frame.empty else frame
        all_rows = predictions[predictions["model_month"].eq(month)]
        if rows.empty:
            result[month] = {"positions": 0, "passed": False}
            continue
        sides = {
            side: {"positions": int(len(group)), "mean_net_bps": float(group["net_bps"].mean())}
            for side, group in rows.groupby("side")
        }
        positives = rows.loc[rows["net_bps"] > 0, "net_bps"]
        positive_total = float(positives.sum())
        concentration = float(positives.max() / positive_total) if positive_total else None
        correlation = float(all_rows["prediction_pct"].corr(all_rows[TARGET]))
        gates = {
            "at_least_100_positions": len(rows) >= 100,
            "positive_net_mean": float(rows["net_bps"].mean()) > 0,
            "profit_factor_above_1_10": (_profit_factor(rows["net_bps"]) or 0) > 1.10,
            "positive_prediction_correlation": correlation > 0,
            "both_directions_non_negative": set(sides) == {"LONG", "SHORT"} and all(
                item["mean_net_bps"] >= 0 for item in sides.values()
            ),
            "single_winner_below_10pct": concentration is not None and concentration < 0.10,
        }
        result[month] = {
            "positions": int(len(rows)),
            "mean_net_bps": float(rows["net_bps"].mean()),
            "profit_factor": _profit_factor(rows["net_bps"]),
            "prediction_correlation": correlation,
            "directions": sides,
            "max_winner_share": concentration,
            "gates": gates,
            "passed": all(gates.values()),
        }
    return result


def run() -> dict:
    # V5 loader omits diagnostic labels by design, so attach the 60m target
    # from the exact same feature partitions after validating row identity.
    samples, audit = load_samples(ROOT / "research_data" / "v5_microstructure_features")
    target_frames = []
    for month in ("2026-05", "2026-06", "2026-07"):
        path = ROOT / "research_data" / "v5_microstructure_features" / f"v5_microstructure_{month}.parquet"
        target_frames.append(pd.read_parquet(path, columns=["decision_time", "symbol", TARGET]))
    targets = pd.concat(target_frames, ignore_index=True)
    targets["decision_time"] = pd.to_datetime(targets["decision_time"], utc=True)
    samples = samples.merge(targets, on=["decision_time", "symbol"], how="left", validate="one_to_one")
    predictions, diagnostics = predict(samples)
    selected = select(predictions)
    monthly = summarize(selected, predictions)
    passed = all(monthly[month].get("passed", False) for month in MONTHS)
    ledger_columns = [
        "decision_time", "symbol", "model_month", "side", "prediction_pct",
        TARGET, "gross_bps", "net_bps",
    ]
    ledger = []
    if not selected.empty:
        for row in selected[ledger_columns].to_dict("records"):
            ledger.append({
                key: value.isoformat() if isinstance(value, pd.Timestamp) else value
                for key, value in row.items()
            })
    return {
        "method": {
            "strategy_id": "V6_FROZEN_AGGTRADE_MICROSTRUCTURE_60M_V1",
            "authority": "RESEARCH_ONLY",
            "target": TARGET,
            "threshold_pct": THRESHOLD_PCT,
            "holding_minutes": HOLD_MINUTES,
            "round_trip_cost_bps": COST_BPS,
        },
        "evidence_audit": audit,
        "diagnostics": diagnostics,
        "monthly": monthly,
        "selected_ledger": ledger,
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "execution_authority": False,
    }


def main() -> int:
    result = run()
    output = ROOT / "research_data" / "v5_microstructure_features" / "v6_frozen_60m_result.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "monthly": result["monthly"]}, indent=2))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
