import numpy as np
import pandas as pd

from scripts.build_v5_microstructure_features import (
    LABELS,
    add_cross_sectional_ranks,
    engineer_symbol_minutes,
)


def _minutes(count=100):
    buckets = pd.date_range("2026-05-01", periods=count, freq="min", tz="UTC")
    price = 100.0 + np.arange(count) * 0.01
    return pd.DataFrame({
        "bucket": buckets,
        "first_price": price,
        "high_price": price + 0.01,
        "low_price": price - 0.01,
        "last_price": price + 0.005,
        "aggregate_trades": 10,
        "total_notional": 1000.0,
        "buy_notional": 600.0,
        "sell_notional": 400.0,
        "signed_notional": 200.0,
        "max_trade_notional": 100.0,
        "aggressor_imbalance": 0.2,
    })


def test_decision_uses_closed_block_and_label_starts_next_block():
    result = engineer_symbol_minutes(_minutes(), "AAAUSDT")
    first = result.iloc[0]
    assert first["decision_time"] == pd.Timestamp("2026-05-01T00:05:00Z")
    assert first["future_entry_price"] == 100.05
    expected = ((100.195 / 100.05) - 1.0) * 100.0
    assert abs(first[LABELS[3]] - expected) < 1e-12


def test_gap_resets_rolling_features_and_forward_labels():
    first = _minutes(100)
    second = _minutes(100)
    second["bucket"] = second["bucket"] + pd.Timedelta(days=2)
    result = engineer_symbol_minutes(pd.concat([first, second]), "AAAUSDT")
    boundary = pd.Timestamp("2026-05-03T00:00:00Z")
    start_second = result[result["block_start"] >= boundary].iloc[0]
    assert pd.isna(start_second["imbalance_15m"])
    last_first = result[result["block_start"] < boundary].iloc[-1]
    assert pd.isna(last_first[LABELS[3]])


def test_ranks_are_built_before_missing_future_label_is_flagged():
    timestamp = pd.Timestamp("2026-05-01T01:00:00Z")
    rows = []
    for symbol, imbalance, label in [
        ("A", 0.1, 1.0), ("B", 0.9, np.nan),
    ]:
        row = {
            "decision_time": timestamp,
            "symbol": symbol,
            "imbalance_5m": imbalance,
            "imbalance_15m": imbalance,
            "flow_z_6h": imbalance,
            "volume_z_6h": imbalance,
            "return_5m_bps": imbalance,
            "return_15m_bps": imbalance,
            "absorption_score": imbalance,
            "continuation_score": imbalance,
            "largest_trade_share_5m": imbalance,
            "total_notional_5m": imbalance,
            LABELS[3]: label,
        }
        rows.append(row)
    ranked = add_cross_sectional_ranks(pd.DataFrame(rows))
    assert ranked.loc[0, "xrank_imbalance_5m"] == 0.5
    assert ranked.loc[1, "xrank_imbalance_5m"] == 1.0
    assert not ranked.loc[1, "primary_label_available"]
