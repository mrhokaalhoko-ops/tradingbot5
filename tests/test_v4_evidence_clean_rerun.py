import numpy as np
import pandas as pd

from scripts.run_v3_walk_forward_ridge import RANK_SOURCES, TARGET
from scripts.run_v4_evidence_clean_rerun import (
    PAST_ELIGIBILITY_COLUMNS,
    load_causal_membership,
    time_rolling_beta,
)


def _row(timestamp, symbol, return_24, target):
    row = {
        "open_time": timestamp,
        "close_time": timestamp + pd.Timedelta(minutes=5),
        "symbol": symbol,
        TARGET: target,
        "global_regime": "RANGE",
    }
    for name in PAST_ELIGIBILITY_COLUMNS:
        row[name] = 1.0
    raw = {
        "return_4_pct": return_24,
        "return_24_pct": return_24,
        "15m_return_1_pct": 0.0,
        "15m_return_4_pct": return_24,
        "15m_return_24_pct": return_24,
        "1h_return_1_pct": 0.0,
        "1h_return_4_pct": return_24,
        "1h_return_24_pct": return_24,
        "ema_alignment": 0.0,
        "15m_ema_alignment": 0.0,
        "1h_ema_alignment": 0.0,
        "ema20_distance_atr": 0.0,
        "15m_ema20_distance_atr": 0.0,
        "1h_ema20_distance_atr": 0.0,
        "atr_pct": 1.0,
        "15m_atr_pct": 1.0,
        "1h_atr_pct": 1.0,
        "volume_ratio": 1.0,
        "15m_volume_ratio": 1.0,
        "1h_volume_ratio": 1.0,
        "close_location": 0.5,
        "body_pct": 0.1,
        "btc_six_hour_return_pct": 0.0,
        "eth_six_hour_return_pct": 0.0,
        "selection_rank": 1.0,
    }
    row.update(raw)
    return row


def test_cross_sectional_membership_is_fixed_before_target_availability(tmp_path):
    timestamp = pd.Timestamp("2026-01-01T00:55:00Z")
    month = tmp_path / "2026-01"
    month.mkdir()
    pd.DataFrame([_row(timestamp, "AAAUSDT", 1.0, 0.5)]).to_parquet(
        month / "AAAUSDT.parquet", index=False
    )
    pd.DataFrame([_row(timestamp, "BBBUSDT", 2.0, np.nan)]).to_parquet(
        month / "BBBUSDT.parquet", index=False
    )
    loaded = load_causal_membership(tmp_path)
    assert len(loaded) == 2
    aaa = loaded[loaded["symbol"] == "AAAUSDT"].iloc[0]
    assert aaa["xrank_return_24_pct"] == 0.5
    assert pd.isna(loaded[loaded["symbol"] == "BBBUSDT"].iloc[0][TARGET])
    assert set(RANK_SOURCES).issubset(loaded.columns)


def test_beta_window_is_clock_time_and_drops_stale_rows_after_gap():
    first = pd.date_range("2026-01-01", periods=48, freq="h", tz="UTC")
    after_gap = pd.DatetimeIndex([first[-1] + pd.Timedelta(hours=169)])
    timestamps = first.append(after_gap)
    values = np.arange(len(timestamps), dtype=float)
    frame = pd.DataFrame({
        "open_time": timestamps,
        "1h_return_1_pct": values,
        "btc_1h_return_pct": values * 0.5,
    })
    beta = time_rolling_beta(frame)
    assert np.isfinite(beta.iloc[47])
    assert pd.isna(beta.iloc[-1])
