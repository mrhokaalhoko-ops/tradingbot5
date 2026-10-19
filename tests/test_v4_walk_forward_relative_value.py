import pandas as pd

from scripts.run_v3_walk_forward_ridge import TARGET
from scripts.run_v4_walk_forward_relative_value import (
    RELATIVE_TARGET,
    prepare_relative_samples,
    select_pairs,
)


def test_relative_label_and_market_features_are_same_timestamp():
    timestamp = pd.Timestamp("2026-01-01T00:55:00Z")
    frame = pd.DataFrame([
        {
            "open_time": timestamp, "symbol": "BTCUSDT",
            "1h_return_1_pct": 1.0, "1h_return_4_pct": 2.0,
            "1h_return_24_pct": 3.0, TARGET: 1.0,
        },
        {
            "open_time": timestamp, "symbol": "AAAUSDT",
            "1h_return_1_pct": 2.0, "1h_return_4_pct": 4.0,
            "1h_return_24_pct": 6.0, TARGET: 3.0,
        },
    ])
    prepared = prepare_relative_samples(frame)
    aaa = prepared[prepared["symbol"] == "AAAUSDT"].iloc[0]
    assert aaa["relative_btc_1h_pct"] == 1.0
    assert aaa["breadth_positive_1h"] == 1.0
    assert aaa[RELATIVE_TARGET] == 1.0


def test_pair_selection_is_atomic_and_charges_both_legs():
    timestamp = pd.Timestamp("2026-06-01T00:55:00Z")
    later = timestamp + pd.Timedelta(hours=1)
    rows = [
        {
            "open_time": timestamp, "symbol": "A", "model_month": "2026-06",
            "evaluation_split": "TEST", "prediction_residual_pct": 0.3,
            TARGET: 1.0,
        },
        {
            "open_time": timestamp, "symbol": "B", "model_month": "2026-06",
            "evaluation_split": "TEST", "prediction_residual_pct": -0.2,
            TARGET: -1.0,
        },
        {
            "open_time": later, "symbol": "A", "model_month": "2026-06",
            "evaluation_split": "TEST", "prediction_residual_pct": 0.4,
            TARGET: 1.0,
        },
        {
            "open_time": later, "symbol": "C", "model_month": "2026-06",
            "evaluation_split": "TEST", "prediction_residual_pct": -0.3,
            TARGET: -1.0,
        },
    ]
    pairs, funnel = select_pairs(pd.DataFrame(rows))
    assert len(pairs) == 1
    assert pairs.iloc[0]["gross_spread_bps"] == 200.0
    assert pairs.iloc[0]["net_pair_bps"] == 176.0
    assert funnel["rejected_symbol_busy"] == 1


def test_pair_below_frozen_spread_is_rejected():
    timestamp = pd.Timestamp("2026-06-01T00:55:00Z")
    frame = pd.DataFrame([
        {
            "open_time": timestamp, "symbol": "A", "model_month": "2026-06",
            "evaluation_split": "TEST", "prediction_residual_pct": 0.10,
            TARGET: 0.4,
        },
        {
            "open_time": timestamp, "symbol": "B", "model_month": "2026-06",
            "evaluation_split": "TEST", "prediction_residual_pct": -0.10,
            TARGET: -0.4,
        },
    ])
    pairs, funnel = select_pairs(frame)
    assert pairs.empty
    assert funnel["rejected_spread_threshold"] == 1
