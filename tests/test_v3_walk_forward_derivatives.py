import numpy as np
import pandas as pd
import pytest

from scripts.run_frozen_derivatives_ranking import DERIVATIVE_FEATURES
from scripts.run_v3_walk_forward_derivatives import merge_derivatives


def test_derivative_join_is_one_to_one_and_sanitizes_infinity():
    timestamp = pd.Timestamp("2026-01-01T00:59:59.999Z")
    base = pd.DataFrame({"close_time": [timestamp], "symbol": ["BTCUSDT"]})
    derivatives = pd.DataFrame({
        "close_time": [timestamp], "symbol": ["BTCUSDT"],
        **{feature: [1.0] for feature in DERIVATIVE_FEATURES},
    })
    derivatives.loc[0, DERIVATIVE_FEATURES[0]] = np.inf
    merged = merge_derivatives(base, derivatives)
    assert pd.isna(merged.loc[0, DERIVATIVE_FEATURES[0]])
    assert merged.loc[0, DERIVATIVE_FEATURES[1]] == 1.0


def test_derivative_join_rejects_duplicate_keys():
    timestamp = pd.Timestamp("2026-01-01T00:59:59.999Z")
    base = pd.DataFrame({"close_time": [timestamp], "symbol": ["BTCUSDT"]})
    row = {
        "close_time": timestamp, "symbol": "BTCUSDT",
        **{feature: 1.0 for feature in DERIVATIVE_FEATURES},
    }
    with pytest.raises(ValueError, match="duplicate"):
        merge_derivatives(base, pd.DataFrame([row, row]))
