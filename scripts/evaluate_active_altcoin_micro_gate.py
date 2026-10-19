"""Evaluate the frozen univariate microstructure screen family.

Research only. Candidate thresholds are derived from Train, eligibility uses
Train and Validation, and the confirmation block is evaluated only after a
single candidate has been selected.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.active_altcoin_scalper import summarize_records  # noqa: E402


LEDGER = ROOT / "research_data" / "active_altcoin_1m" / "replay_ledger.parquet"
REPLAY_JSON = ROOT / "research_data" / "active_altcoin_1m" / "replay_result.json"
OUTPUT = ROOT / "research_data" / "active_altcoin_1m" / "micro_gate_result.json"
PROTOCOL = "ACTIVE_ALTCOIN_MICRO_GATE_PROTOCOL_2026-09-04.md"

TRAIN_MONTHS = tuple(f"2025-{month:02d}" for month in range(8, 13)) + tuple(
    f"2026-{month:02d}" for month in range(1, 4)
)
VALIDATION_MONTHS = ("2026-04", "2026-05")
TEST_MONTHS = ("2026-06", "2026-07")
FEATURES = (
    "micro_directional_5m_return_pct",
    "micro_directional_bar_fraction",
    "micro_close_location_mean",
    "micro_last_close_location",
    "micro_volume_concentration",
    "micro_last_volume_ratio",
    "micro_range_atr",
    "micro_signed_return_dispersion",
    "micro_directional_taker_imbalance_mean",
    "micro_last_directional_taker_imbalance",
    "micro_flow_price_agreement",
    "micro_directional_absorption",
)


def _summary(frame: pd.DataFrame) -> dict:
    return summarize_records(frame.to_dict("records"))


def _month_checks(frame: pd.DataFrame, months: tuple[str, ...]) -> dict:
    checks = {}
    for month in months:
        subset = frame.loc[frame["month"].eq(month)]
        summary = _summary(subset)
        checks[month] = {
            "positions": int(summary.get("positions", 0)),
            "expectancy_r": summary.get("expectancy_r"),
            "positive": bool(
                summary.get("positions", 0) > 0
                and summary.get("expectancy_r", float("-inf")) > 0
            ),
        }
    return checks


def _condition(frame: pd.DataFrame, feature: str, operator: str, threshold: float) -> pd.Series:
    values = pd.to_numeric(frame[feature], errors="coerce")
    return values.le(threshold) if operator == "LE" else values.ge(threshold)


def _cluster_bootstrap_expectancy(
    frame: pd.DataFrame,
    cluster: pd.Series,
    *,
    samples: int = 5_000,
    seed: int = 20260904,
) -> dict:
    """Bootstrap expectancy while keeping correlated cluster rows together."""
    work = pd.DataFrame({
        "cluster": cluster.astype(str).to_numpy(),
        "net_r": pd.to_numeric(frame["net_r"], errors="coerce").to_numpy(),
    }).dropna(subset=["net_r"])
    grouped = work.groupby("cluster", sort=True)["net_r"].agg(["sum", "count"])
    if grouped.empty:
        return {"clusters": 0, "positions": 0, "mean_r": None}
    sums = grouped["sum"].to_numpy(float)
    counts = grouped["count"].to_numpy(float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(grouped), size=(int(samples), len(grouped)))
    sampled_mean = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    lower, median, upper = np.quantile(sampled_mean, [0.025, 0.50, 0.975])
    return {
        "clusters": int(len(grouped)),
        "positions": int(len(work)),
        "mean_r": float(work["net_r"].mean()),
        "ci95_low_r": float(lower),
        "bootstrap_median_r": float(median),
        "ci95_high_r": float(upper),
        "probability_expectancy_positive": float((sampled_mean > 0).mean()),
        "samples": int(samples),
    }


def _uncertainty(frame: pd.DataFrame, *, seed: int) -> dict:
    signal_time = pd.to_datetime(frame["signal_time"], utc=True)
    return {
        "utc_day_cluster": _cluster_bootstrap_expectancy(
            frame, signal_time.dt.strftime("%Y-%m-%d"), seed=seed
        ),
        "symbol_cluster": _cluster_bootstrap_expectancy(
            frame, frame["symbol"], seed=seed + 1
        ),
    }


def evaluate() -> dict:
    if LEDGER.exists():
        ledger = pd.read_parquet(LEDGER)
    else:
        payload = json.loads(REPLAY_JSON.read_text(encoding="utf-8"))
        ledger = pd.DataFrame(payload["ledger"])
    ledger = ledger.loc[
        ledger["variant"].eq("PULLBACK_RECLAIM_1M")
        & pd.to_numeric(ledger["net_r"], errors="coerce").notna()
    ].copy()
    ledger["net_r"] = pd.to_numeric(ledger["net_r"], errors="coerce")
    train = ledger.loc[ledger["month"].isin(TRAIN_MONTHS)].copy()
    validation = ledger.loc[ledger["month"].isin(VALIDATION_MONTHS)].copy()
    confirmation = ledger.loc[ledger["month"].isin(TEST_MONTHS)].copy()

    candidates = []
    for feature in FEATURES:
        train_values = pd.to_numeric(train[feature], errors="coerce").dropna()
        if train_values.empty:
            continue
        quantiles = train_values.quantile([0.33, 0.50, 0.67]).to_dict()
        specs = (
            ("LE", "P33", quantiles[0.33]),
            ("LE", "P50", quantiles[0.50]),
            ("GE", "P50", quantiles[0.50]),
            ("GE", "P67", quantiles[0.67]),
        )
        for operator, quantile, threshold in specs:
            train_slice = train.loc[_condition(train, feature, operator, threshold)]
            validation_slice = validation.loc[
                _condition(validation, feature, operator, threshold)
            ]
            train_summary = _summary(train_slice)
            validation_summary = _summary(validation_slice)
            train_months = _month_checks(train_slice, TRAIN_MONTHS)
            validation_months = _month_checks(validation_slice, VALIDATION_MONTHS)
            positive_train_months = sum(row["positive"] for row in train_months.values())
            eligible = bool(
                train_summary.get("positions", 0) >= 120
                and validation_summary.get("positions", 0) >= 40
                and train_summary.get("expectancy_r", float("-inf")) > 0
                and validation_summary.get("expectancy_r", float("-inf")) > 0
                and train_summary.get("profit_factor", 0) > 1.05
                and validation_summary.get("profit_factor", 0) > 1.05
                and positive_train_months >= 6
                and all(
                    row["positions"] >= 10 and row["positive"]
                    for row in validation_months.values()
                )
            )
            candidates.append({
                "feature": feature,
                "operator": operator,
                "quantile": quantile,
                "threshold": float(threshold),
                "eligible": eligible,
                "train": train_summary,
                "validation": validation_summary,
                "positive_train_months": int(positive_train_months),
                "train_months": train_months,
                "validation_months": validation_months,
            })

    eligible = [row for row in candidates if row["eligible"]]
    eligible.sort(
        key=lambda row: (
            -min(row["train"]["expectancy_r"], row["validation"]["expectancy_r"]),
            -row["validation"]["positions"],
            row["feature"],
            row["operator"],
        )
    )
    selected = None
    if eligible:
        selected = dict(eligible[0])
        mask = _condition(
            confirmation,
            selected["feature"],
            selected["operator"],
            selected["threshold"],
        )
        selected["confirmation"] = _summary(confirmation.loc[mask])
        selected["confirmation_months"] = _month_checks(
            confirmation.loc[mask], TEST_MONTHS
        )
        train_selected = train.loc[
            _condition(train, selected["feature"], selected["operator"], selected["threshold"])
        ]
        validation_selected = validation.loc[
            _condition(validation, selected["feature"], selected["operator"], selected["threshold"])
        ]
        confirmation_selected = confirmation.loc[mask]
        selected["uncertainty"] = {
            "train": _uncertainty(train_selected, seed=20260904),
            "validation": _uncertainty(validation_selected, seed=20260914),
            "confirmation": _uncertainty(confirmation_selected, seed=20260924),
        }

    ranked_diagnostic = sorted(
        candidates,
        key=lambda row: (
            -min(
                row["train"].get("expectancy_r", float("-inf")),
                row["validation"].get("expectancy_r", float("-inf")),
            ),
            -row["validation"].get("positions", 0),
        ),
    )[:10]
    return {
        "method": {
            "strategy_id": "ACTIVE_ALTCOIN_MICRO_GATE_V1",
            "protocol": PROTOCOL,
            "authority": "RESEARCH_ONLY",
            "candidate_count": len(candidates),
            "test_used_for_selection": False,
        },
        "parent": {
            "train": _summary(train),
            "validation": _summary(validation),
            "confirmation": _summary(confirmation),
        },
        "eligible_count": len(eligible),
        "selected": selected,
        "ranked_train_validation_diagnostic": ranked_diagnostic,
        "verdict": "PROSPECTIVE_SHADOW_CANDIDATE" if selected else "NO_GATE",
        "execution_authority": False,
    }


def main() -> int:
    result = evaluate()
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "parent": result["parent"],
        "eligible_count": result["eligible_count"],
        "selected": result["selected"],
        "verdict": result["verdict"],
    }, ensure_ascii=True))
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
