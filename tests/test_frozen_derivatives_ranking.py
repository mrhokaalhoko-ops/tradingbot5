import numpy as np
import pandas as pd
import pytest

from scripts.run_frozen_derivatives_ranking import (
    DERIVATIVE_FEATURES,
    merge_derivative_samples,
)


def _base():
    return pd.DataFrame({
        "close_time": pd.to_datetime(["2026-01-01T03:59:59.999Z"]),
        "symbol": ["BTCUSDT"],
        "split": ["TRAIN"],
    })


def test_derivative_merge_is_one_to_one_and_sanitizes_infinity():
    derivatives = pd.DataFrame({
        "close_time": pd.to_datetime(["2026-01-01T03:59:59.999Z"]),
        "symbol": ["BTCUSDT"],
        **{feature: [1.0] for feature in DERIVATIVE_FEATURES},
    })
    derivatives.loc[0, DERIVATIVE_FEATURES[0]] = np.inf
    result = merge_derivative_samples(_base(), derivatives)
    assert pd.isna(result.iloc[0][DERIVATIVE_FEATURES[0]])
    assert result.iloc[0][DERIVATIVE_FEATURES[1]] == 1.0


def test_derivative_merge_rejects_duplicate_timestamp_symbol():
    row = {
        "close_time": pd.Timestamp("2026-01-01T03:59:59.999Z"),
        "symbol": "BTCUSDT",
        **{feature: 1.0 for feature in DERIVATIVE_FEATURES},
    }
    with pytest.raises(ValueError, match="duplicate"):
        merge_derivative_samples(_base(), pd.DataFrame([row, row]))
