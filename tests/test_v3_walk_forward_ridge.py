import pandas as pd

from scripts.run_v3_walk_forward_ridge import (
    HOLD_HOURS,
    add_cross_sectional_ranks,
    evaluation_split,
    select_portfolio,
    training_window,
)


def test_training_window_is_past_only_with_seven_day_embargo():
    start, end, prediction_end = training_window("2026-06")
    assert start == pd.Timestamp("2025-11-26T00:00:00Z")
    assert end == pd.Timestamp("2026-05-25T00:00:00Z")
    assert prediction_end == pd.Timestamp("2026-07-01T00:00:00Z")
    assert evaluation_split(pd.Timestamp("2026-04-01T00:00:00Z")) == "VALIDATION"
    assert evaluation_split(pd.Timestamp("2026-06-01T00:00:00Z")) == "TEST"


def test_cross_sectional_rank_uses_only_same_timestamp():
    frame = pd.DataFrame({
        "open_time": pd.to_datetime([
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
            "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z",
        ]),
        **{
            name: [1.0, 2.0, 100.0, 50.0]
            for name in [
                "return_4_pct", "return_24_pct", "15m_return_4_pct",
                "15m_return_24_pct", "1h_return_4_pct", "1h_return_24_pct",
                "ema20_distance_atr", "15m_ema20_distance_atr",
                "1h_ema20_distance_atr", "atr_pct", "volume_ratio",
                "15m_volume_ratio", "1h_volume_ratio",
            ]
        },
    })
    ranked = add_cross_sectional_ranks(frame)
    assert ranked.loc[0, "xrank_return_24_pct"] == 0.5
    assert ranked.loc[1, "xrank_return_24_pct"] == 1.0
    assert ranked.loc[2, "xrank_return_24_pct"] == 1.0
    assert ranked.loc[3, "xrank_return_24_pct"] == 0.5


def test_portfolio_takes_one_each_direction_and_respects_symbol_busy():
    time = pd.Timestamp("2026-06-01T00:00:00Z")
    later = time + pd.Timedelta(hours=1)
    frame = pd.DataFrame([
        {"open_time": time, "symbol": "A", "prediction_pct": 0.8,
         "future_close_48x5m_from_entry_pct": 1.0},
        {"open_time": time, "symbol": "B", "prediction_pct": 0.5,
         "future_close_48x5m_from_entry_pct": 1.0},
        {"open_time": time, "symbol": "C", "prediction_pct": -0.9,
         "future_close_48x5m_from_entry_pct": -1.0},
        {"open_time": later, "symbol": "A", "prediction_pct": 0.9,
         "future_close_48x5m_from_entry_pct": 1.0},
    ])
    selected, funnel = select_portfolio(frame)
    assert set(selected.iloc[:2]["symbol"]) == {"A", "C"}
    assert len(selected) == 2
    assert funnel["rejected_symbol_busy"] == 1
    assert HOLD_HOURS == 4
