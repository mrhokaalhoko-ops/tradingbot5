import pandas as pd

from scripts.replay_causal_rule_candidates import _execution_month_names


def test_execution_month_names_includes_cross_month_horizon_only():
    frame = pd.DataFrame({
        "close_time": pd.to_datetime([
            "2026-01-15T12:00:00Z",
            "2026-01-31T23:00:00Z",
            "2026-03-01T00:00:00Z",
        ])
    })

    assert _execution_month_names(frame, horizon_hours=4) == [
        "2026-01", "2026-02", "2026-03",
    ]
