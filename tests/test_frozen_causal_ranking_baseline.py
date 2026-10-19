import numpy as np
import pandas as pd
import pytest

from scripts.run_frozen_causal_ranking_baseline import (
    NUMERIC_FEATURES,
    TARGET,
    build_model,
    sanitize_samples,
    select_ranked,
    split_name,
    summarize_selected,
)


def test_calendar_splits_are_fixed():
    assert split_name(pd.Timestamp("2025-08-01T00:00:00Z")) == "TRAIN"
    assert split_name(pd.Timestamp("2026-03-31T23:59:00Z")) == "TRAIN"
    assert split_name(pd.Timestamp("2026-04-01T00:00:00Z")) == "VALIDATION"
    assert split_name(pd.Timestamp("2026-06-01T00:00:00Z")) == "TEST"
    assert split_name(pd.Timestamp("2026-08-01T00:00:00Z")) is None


def test_selection_is_top_three_per_timestamp_and_charges_directional_cost():
    times = [pd.Timestamp("2026-06-01T03:55:00Z")] * 5
    frame = pd.DataFrame({
        "open_time": times,
        "symbol": ["A", "B", "C", "D", "E"],
        "prediction_pct": [0.5, -0.8, 0.1, 0.4, -0.3],
        "future_close_48x5m_from_entry_pct": [1.0, -1.0, 2.0, -0.5, 0.2],
    })
    selected = select_ranked(frame)
    assert selected["symbol"].tolist() == ["A", "B", "D"]
    values = dict(zip(selected["symbol"], selected["net_bps"]))
    assert values["A"] == pytest.approx(88.0)
    assert values["B"] == pytest.approx(88.0)
    assert values["D"] == pytest.approx(-62.0)


def test_summary_reports_month_stability_and_profit_factor():
    frame = pd.DataFrame({
        "open_time": pd.to_datetime([
            "2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z",
        ]),
        "net_bps": [20.0, -10.0],
    })
    result = summarize_selected(frame)
    assert result["mean_net_bps"] == pytest.approx(5.0)
    assert result["profit_factor"] == pytest.approx(2.0)
    assert result["by_month"]["2026-06"]["total_net_bps"] == pytest.approx(20.0)


def test_numeric_feature_list_is_unique_for_deterministic_preprocessing():
    assert len(NUMERIC_FEATURES) == len(set(NUMERIC_FEATURES))
    custom = build_model([NUMERIC_FEATURES[0]])
    assert custom.named_steps["preprocess"].transformers[0][2] == [NUMERIC_FEATURES[0]]


def test_sanitize_samples_converts_infinity_and_drops_invalid_target():
    rows = pd.DataFrame({name: [1.0, 1.0] for name in NUMERIC_FEATURES})
    rows.loc[0, NUMERIC_FEATURES[0]] = np.inf
    rows[TARGET] = [1.0, np.inf]
    clean = sanitize_samples(rows)
    assert len(clean) == 1
    assert pd.isna(clean.iloc[0][NUMERIC_FEATURES[0]])
