from pathlib import Path

import pandas as pd

from scripts.profile_causal_dataset import profile


def test_profile_accepts_feature_dataset_schema(tmp_path: Path):
    root = tmp_path / "features" / "2026-01"
    root.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "open_time": pd.to_datetime(["2026-01-01 00:55:00"], utc=True),
            "symbol": ["TESTUSDT"],
            "global_regime": ["RANGE"],
            "observation_valid": [True],
            "return_24_pct": [10.0],
            "atr_pct": [1.0],
            "future_close_48x5m_from_entry_pct": [1.0],
            "future_high_48x5m_from_entry_pct": [3.0],
            "future_low_48x5m_from_entry_pct": [-1.0],
        }
    )
    frame.to_parquet(root / "TESTUSDT.parquet", index=False)

    result = profile(tmp_path / "features", (10.0,))

    assert result["rows"]["sampled"] == 1
    assert result["groups"]["abs24_ge_10pct|RANGE|BUY"]["events"] == 1
