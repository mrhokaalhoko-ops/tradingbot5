import numpy as np
import pandas as pd

from scripts.run_v5_frozen_microstructure import (
    FEATURES,
    TARGET,
    select_portfolio,
    training_bounds,
)


def test_feature_allow_list_contains_no_future_or_identity_fields():
    assert "symbol" not in FEATURES
    assert "feature_ready" not in FEATURES
    assert "primary_label_available" not in FEATURES
    assert not any(name.startswith("future_") for name in FEATURES)


def test_frozen_training_bounds_embargo_last_prior_month_day():
    assert training_bounds("2026-06") == (
        pd.Timestamp("2026-05-01T00:00:00Z"),
        pd.Timestamp("2026-05-31T00:00:00Z"),
    )
    assert training_bounds("2026-07") == (
        pd.Timestamp("2026-06-01T00:00:00Z"),
        pd.Timestamp("2026-06-30T00:00:00Z"),
    )


def test_portfolio_applies_direction_cost_and_symbol_overlap():
    rows = []
    for timestamp in pd.date_range("2026-06-01", periods=2, freq="5min", tz="UTC"):
        rows.extend([
            {
                "decision_time": timestamp,
                "symbol": "AAAUSDT",
                "prediction_pct": 0.40,
                TARGET: 0.30,
                "model_month": "2026-06",
                "evaluation_split": "VALIDATION",
            },
            {
                "decision_time": timestamp,
                "symbol": "BBBUSDT",
                "prediction_pct": -0.30,
                TARGET: -0.25,
                "model_month": "2026-06",
                "evaluation_split": "VALIDATION",
            },
        ])
    selected, funnel = select_portfolio(pd.DataFrame(rows))
    assert len(selected) == 2
    assert set(selected["side"]) == {"LONG", "SHORT"}
    assert np.allclose(sorted(selected["net_bps"]), [13.0, 18.0])
    assert funnel["rejected_symbol_busy"] == 2
